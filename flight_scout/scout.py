"""`search_flights()` — the spec section 6 tool, backed by a real browser.

    result = await search_flights("BLR", "DEL", "2026-09-15")

Build the URL, let Cleartrip's own page do the search, capture the response it
fetches, parse it. No extension, no scraping of rendered HTML, no reverse-
engineered API call of our own — the site does exactly what it does for a person,
and we read the answer.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import cleartrip
from .capture import Capture, CaptureError, capture_xhr, ensure_chrome

CHROME_PORT = int(os.environ.get("SCOUT_CHROME_PORT", "9223"))
PROFILE_DIR = Path(
    os.environ.get("SCOUT_PROFILE_DIR", Path.home() / ".hands-free" / "scout-profile")
)
CACHE_TTL_S = float(os.environ.get("SCOUT_CACHE_TTL_S", "900"))


@dataclass
class SearchResult:
    ok: bool
    flights: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    error_kind: str | None = None
    elapsed_ms: int | None = None
    cached: bool = False
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def as_record(self, origin: str, destination: str, depart_date: str,
                  return_date: str | None = None) -> dict[str, Any]:
        """Shape `flight_bridge.normalize` consumes, so one normalizer serves
        both this path and the extension path."""
        return {
            "id": f"scout-{int(time.time())}",
            "route": {
                "from": origin.upper(),
                "to": destination.upper(),
                "date": depart_date,
                "returnDate": return_date,
            },
            "sites": {
                "cleartrip": {
                    "status": "complete" if self.ok else "error",
                    "error": self.error,
                    "flights": self.flights,
                }
            },
        }


_cache: dict[tuple, tuple[float, SearchResult]] = {}
# One search at a time. Two concurrent navigations would fight over the same tab,
# and a prefetch must never disrupt a search someone is waiting on.
_lock = asyncio.Lock()


def _cache_key(*args: Any) -> tuple:
    return tuple(str(a).upper() if isinstance(a, str) else a for a in args)


async def search_flights(
    origin: str,
    destination: str,
    depart_date: str,
    *,
    return_date: str | None = None,
    adults: int = 1,
    children: int = 0,
    infants: int = 0,
    cabin: str = "ECONOMY",
    timeout_s: float = 90.0,
    use_cache: bool = True,
) -> SearchResult:
    key = _cache_key(origin, destination, depart_date, return_date, adults, children, infants, cabin)

    if use_cache and (hit := _cache.get(key)) is not None:
        at, result = hit
        if (time.monotonic() - at) < CACHE_TTL_S:
            # The path that makes a speculative prefetch feel instant.
            return SearchResult(
                ok=result.ok,
                flights=result.flights,
                error=result.error,
                error_kind=result.error_kind,
                elapsed_ms=0,
                cached=True,
                diagnostics=result.diagnostics,
            )
        del _cache[key]

    url = cleartrip.search_url(
        origin, destination, depart_date,
        return_date=return_date, adults=adults, children=children,
        infants=infants, cabin=cabin,
    )

    started = time.monotonic()
    async with _lock:
        try:
            ensure_chrome(port=CHROME_PORT, profile_dir=PROFILE_DIR)
        except CaptureError as exc:
            return SearchResult(ok=False, error=str(exc), error_kind="no_browser")

        try:
            cap: Capture = await capture_xhr(
                navigate_to=url,
                url_pattern=cleartrip.SEARCH_API_PATTERN,
                port=CHROME_PORT,
                timeout_s=timeout_s,
            )
        except CaptureError as exc:
            # Most likely a bot challenge or a very slow page. Either way it is a
            # typed failure the agent reports as "couldn't fetch" — it must never
            # be turned into invented results (spec section 10).
            return SearchResult(
                ok=False,
                error=str(exc),
                error_kind="capture_failed",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    parsed = cleartrip.parse(cap.body, expect_destination=destination)
    elapsed = int((time.monotonic() - started) * 1000)

    result = SearchResult(
        ok=parsed["ok"],
        flights=parsed["flights"],
        error=parsed.get("error"),
        error_kind=None if parsed["ok"] else "no_results",
        elapsed_ms=elapsed,
        diagnostics={
            **parsed.get("diagnostics", {}),
            "captured_bytes": len(cap.body),
            "http_status": cap.status,
            "requests_seen": cap.requests_seen,
            "capture_ms": cap.elapsed_ms,
        },
    )

    if result.ok:
        _cache[key] = (time.monotonic(), result)
    return result


async def _demo() -> None:
    import sys

    origin = sys.argv[1] if len(sys.argv) > 1 else "BLR"
    dest = sys.argv[2] if len(sys.argv) > 2 else "DEL"
    date = sys.argv[3] if len(sys.argv) > 3 else "2026-09-15"

    print(f"searching {origin} -> {dest} on {date} …")
    res = await search_flights(origin, dest, date)
    print(f"ok={res.ok} elapsed={res.elapsed_ms}ms cached={res.cached}")
    print(f"diagnostics: {res.diagnostics}")
    if res.error:
        print("error:", res.error_kind, res.error)
    for f in res.flights[:5]:
        print(
            f"  Rs {int(f['minPrice']):>7,}  {f['departIso'][11:16]}-{f['arriveIso'][11:16]}  "
            f"{'/'.join(f['flightNumbers']):<16} {', '.join(f['airlines'])[:24]:<24} "
            f"{f['stops']} stop"
        )


if __name__ == "__main__":
    asyncio.run(_demo())
