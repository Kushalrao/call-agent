"""Flight bridge: the agent asks for a route, real Chrome answers.

Why a bridge at all, instead of the agent scraping directly: the Watermelon
extension works *because* it runs inside the user's real Chrome with real
cookies and a real session. That is what gets past the aggregators' anti-bot
defences, and it is not reproducible from a headless browser or a server-side
HTTP client. So the browser stays in the loop, and this service is the queue
between it and the agent.

    agent worker                bridge (this)              Chrome + extension
    ------------                -------------              ------------------
    POST /v1/search  ------->   enqueue job
                                (holds request)   <------  GET /v1/jobs/next
                                                          opens Cleartrip tab,
                                                          cross-searches others
                                <-------------------       POST /v1/jobs/{id}/result
    <---- normalized results    normalize + cache

Binds to loopback only, and every endpoint requires a shared token: this
service can make a browser navigate, which is not a capability to leave open on
a network interface.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from .normalize import to_widget_payload

# --- configuration ----------------------------------------------------------

BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN") or secrets.token_urlsafe(24)
# A browser-driven cross-search is measured in seconds, not milliseconds — the
# extension's own notes record 22.3s for one cloud run. These are deliberately
# generous.
JOB_TIMEOUT_S = float(os.environ.get("BRIDGE_JOB_TIMEOUT_S", "75"))
CACHE_TTL_S = float(os.environ.get("BRIDGE_CACHE_TTL_S", "900"))  # 15 min
POLL_HOLD_S = float(os.environ.get("BRIDGE_POLL_HOLD_S", "25"))


# --- models -----------------------------------------------------------------


class SearchRequest(BaseModel):
    """Mirrors spec section 6's `search_flights` signature."""

    origin: str = Field(min_length=3, max_length=3)
    destination: str = Field(min_length=3, max_length=3)
    depart_date: str  # ISO yyyy-mm-dd
    return_date: str | None = None
    adults: int = Field(default=1, ge=1, le=9)
    children: int = Field(default=0, ge=0, le=8)
    infants: int = Field(default=0, ge=0, le=8)
    cabin: Literal["ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"] = "ECONOMY"

    # Prefetch requests are speculative: nobody is waiting, so they must never
    # block a real one and may be dropped under load.
    speculative: bool = False

    def to_route(self) -> dict[str, Any]:
        """The extension's normalized Route shape (lib/route.js)."""
        return {
            "from": self.origin.upper(),
            "to": self.destination.upper(),
            "date": self.depart_date,
            "returnDate": self.return_date,
            "adults": self.adults,
            "children": self.children,
            "infants": self.infants,
            "cabin": self.cabin,
            "tripType": "ROUND_TRIP" if self.return_date else "ONE_WAY",
            # `intl` is deliberately omitted: lib/route.js derives it from the
            # airport codes, and Cleartrip 400s when the flag disagrees with the
            # route. Let the code that owns that logic own it.
        }

    @property
    def cache_key(self) -> str:
        raw = "|".join(
            [
                self.origin.upper(),
                self.destination.upper(),
                self.depart_date,
                self.return_date or "",
                str(self.adults),
                str(self.children),
                str(self.infants),
                self.cabin,
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


class SearchError(BaseModel):
    """Typed failure. The agent surfaces this as "couldn't fetch flights" and
    must never invent results (spec section 10)."""

    kind: Literal["timeout", "no_browser", "no_results", "all_platforms_failed"]
    detail: str


class SearchResponse(BaseModel):
    ok: bool
    cached: bool = False
    elapsed_ms: int | None = None
    payload: dict[str, Any] | None = None
    error: SearchError | None = None


# --- job queue --------------------------------------------------------------


@dataclass
class Job:
    id: str
    request: SearchRequest
    created_at: float = field(default_factory=time.monotonic)
    claimed_at: float | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    record: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class CacheEntry:
    payload: dict[str, Any]
    at: float

    @property
    def age_s(self) -> float:
        return time.monotonic() - self.at


class Bridge:
    def __init__(self) -> None:
        self.pending: asyncio.Queue[Job] = asyncio.Queue()
        self.jobs: dict[str, Job] = {}
        self.cache: dict[str, CacheEntry] = {}
        # One in-flight job per route. Without this, a prefetch and a real
        # request for the same route would open two Cleartrip tabs and race.
        self.inflight: dict[str, Job] = {}
        self.last_browser_seen: float | None = None

    @property
    def browser_connected(self) -> bool:
        """True if the extension polled recently. Used to fail fast with
        `no_browser` rather than making the agent wait out the full timeout when
        Chrome simply isn't running."""
        if self.last_browser_seen is None:
            return False
        return (time.monotonic() - self.last_browser_seen) < (POLL_HOLD_S * 2 + 10)

    def cached(self, key: str) -> CacheEntry | None:
        entry = self.cache.get(key)
        if entry is None:
            return None
        if entry.age_s > CACHE_TTL_S:
            del self.cache[key]
            return None
        return entry


bridge = Bridge()


# --- auth -------------------------------------------------------------------


async def require_token(x_bridge_token: str = Header(default="")) -> None:
    if not secrets.compare_digest(x_bridge_token, BRIDGE_TOKEN):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad bridge token")


# --- app --------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    print(f"[bridge] listening. BRIDGE_TOKEN={BRIDGE_TOKEN}")
    print("[bridge] put that token in the extension's bridge config.")
    yield


app = FastAPI(title="hands-free flight bridge", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "browser_connected": bridge.browser_connected,
        "queued": bridge.pending.qsize(),
        "inflight": len(bridge.inflight),
        "cached_routes": len(bridge.cache),
    }


# --- agent side -------------------------------------------------------------


@app.post("/v1/search", response_model=SearchResponse, dependencies=[Depends(require_token)])
async def search(req: SearchRequest) -> SearchResponse:
    started = time.monotonic()
    key = req.cache_key

    if (entry := bridge.cached(key)) is not None:
        # This is the path that makes the agent feel instant: a speculative
        # prefetch ran earlier, so the answer is already here.
        return SearchResponse(
            ok=True, cached=True, elapsed_ms=0, payload=entry.payload
        )

    if not bridge.browser_connected:
        return SearchResponse(
            ok=False,
            error=SearchError(
                kind="no_browser",
                detail="no Chrome with the extension is polling the bridge",
            ),
        )

    # Join an in-flight job for the same route rather than starting a second one.
    job = bridge.inflight.get(key)
    if job is None:
        job = Job(id=secrets.token_urlsafe(10), request=req)
        bridge.jobs[job.id] = job
        bridge.inflight[key] = job
        await bridge.pending.put(job)

    try:
        await asyncio.wait_for(job.done.wait(), timeout=JOB_TIMEOUT_S)
    except asyncio.TimeoutError:
        bridge.inflight.pop(key, None)
        return SearchResponse(
            ok=False,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=SearchError(
                kind="timeout", detail=f"browser did not return within {JOB_TIMEOUT_S:.0f}s"
            ),
        )

    bridge.inflight.pop(key, None)
    elapsed = int((time.monotonic() - started) * 1000)

    if job.error or job.record is None:
        return SearchResponse(
            ok=False,
            elapsed_ms=elapsed,
            error=SearchError(kind="all_platforms_failed", detail=job.error or "no record"),
        )

    payload = to_widget_payload(job.record)
    # Per-platform now: usable if ANY platform returned something. A single
    # platform timing out is normal and must not fail the whole search.
    usable = {
        name: p for name, p in payload["platforms"].items() if p["options"]
    }
    if not usable:
        summary = {
            name: {"status": p["status"], "error": p["error"]}
            for name, p in payload["platforms"].items()
        }
        return SearchResponse(
            ok=False,
            elapsed_ms=elapsed,
            error=SearchError(kind="no_results", detail=f"no platform returned flights; {summary}"),
        )

    bridge.cache[key] = CacheEntry(payload=payload, at=time.monotonic())
    return SearchResponse(ok=True, cached=False, elapsed_ms=elapsed, payload=payload)


# --- extension side ---------------------------------------------------------


class JobOffer(BaseModel):
    job_id: str | None = None
    route: dict[str, Any] | None = None
    entry_site: str = "cleartrip"


@app.get("/v1/jobs/next", response_model=JobOffer, dependencies=[Depends(require_token)])
async def next_job() -> JobOffer:
    """Long-poll for work.

    Holding the request open rather than returning empty immediately keeps the
    extension's service worker from waking on a tight timer, and gets a job to
    Chrome within milliseconds of it being queued.
    """
    bridge.last_browser_seen = time.monotonic()
    try:
        job = await asyncio.wait_for(bridge.pending.get(), timeout=POLL_HOLD_S)
    except asyncio.TimeoutError:
        return JobOffer()

    job.claimed_at = time.monotonic()
    bridge.last_browser_seen = time.monotonic()
    return JobOffer(job_id=job.id, route=job.request.to_route(), entry_site="cleartrip")


class JobResult(BaseModel):
    record: dict[str, Any] | None = None
    error: str | None = None


@app.post("/v1/jobs/{job_id}/result", dependencies=[Depends(require_token)])
async def post_result(job_id: str, body: JobResult) -> dict[str, Any]:
    bridge.last_browser_seen = time.monotonic()
    job = bridge.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    if job.done.is_set():
        return {"ok": True, "note": "already completed"}

    job.record = body.record
    job.error = body.error
    job.done.set()
    return {"ok": True}


@app.post("/v1/jobs/{job_id}/heartbeat", dependencies=[Depends(require_token)])
async def heartbeat(job_id: str) -> dict[str, Any]:
    """The extension reports that a job is still progressing, so a slow
    cross-search is distinguishable from a dead browser."""
    bridge.last_browser_seen = time.monotonic()
    return {"ok": job_id in bridge.jobs}
