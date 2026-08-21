"""HTTP surface for the flight search, so an ElevenLabs agent can call it.

The agent runs on ElevenLabs' infrastructure and the search runs here, driving a
real Chrome with the user's own extension. So the search has to be reachable from
the public internet — which means this is a tiny, deliberately boring service with
exactly one capability and a shared secret in front of it.

Two things it is careful about.

**The response is written to be spoken.** The extension returns hundreds of
flights; a voice agent needs one sentence and a couple of alternatives. Handing a
model a large JSON blob and hoping it summarises well is how you get a agent that
reads out fare codes, so the shaping happens here where it can be tested.

**It never hangs.** A voice conversation cannot wait indefinitely, so the search
has a hard deadline and a failed search returns a speakable sentence rather than
an error the model has to interpret.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from agent.airports import describe
from agent.places import resolve, spoken_name
from agent.resolver import resolve_with_model
from agent.search import (
    DEFAULT_ORIGIN,
    SearchCache,
    default_depart_date,
    run_search,
    to_sentence,
)
from control_plane.logging_setup import log_event

# ElevenLabs sends this as a header on every tool call. Without it the tunnel is
# an open endpoint that drives a real browser on someone's laptop.
TOOL_SECRET = os.environ.get("FLIGHT_TOOL_SECRET", "")

app = FastAPI(title="hands-free flight tool", version="1.0.0")

# Shared across calls: the aggregators throttle repeated identical queries, so a
# route fetched once in the last few minutes is answered from here.
_cache = SearchCache()
_cached_at: dict[str, float] = {}
CACHE_TTL_S = 600.0


class SearchRequest(BaseModel):
    destination: str = Field(description="City as spoken, e.g. 'Bali' or 'Dubai'")
    origin: str | None = Field(default=None, description="City as spoken; defaults to Bangalore")
    depart_date: str | None = Field(default=None, description="ISO yyyy-mm-dd; defaults to today + 10 days")


class Option(BaseModel):
    carrier: str
    price_inr: int
    stops: int
    platform: str


class SearchResponse(BaseModel):
    """Deliberately small. `say` is what the agent should read out."""

    say: str
    found: bool
    origin: str
    destination: str
    depart_date: str
    cheapest: Option | None = None
    alternatives: list[Option] = []
    took_seconds: float


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _check(secret: str | None) -> None:
    if not TOOL_SECRET:
        raise HTTPException(500, "FLIGHT_TOOL_SECRET is not configured")
    if secret != TOOL_SECRET:
        raise HTTPException(401, "bad or missing tool secret")


@app.post("/tool/search_flights", response_model=SearchResponse)
async def search_flights(
    body: SearchRequest,
    x_tool_secret: str | None = Header(default=None, alias="X-Tool-Secret"),
) -> SearchResponse:
    _check(x_tool_secret)

    destination = resolve(body.destination)
    origin = resolve(body.origin) if body.origin else DEFAULT_ORIGIN
    # Resolve the route first: the default lead time depends on whether it is
    # domestic, so the date cannot be chosen before the cities are known.
    depart = body.depart_date

    # Nothing matched by name. Before giving up, ask a model to turn what they
    # said into city names — countries, landmarks and descriptions are language,
    # not data. Whatever it suggests is looked up in the same airport dataset, so
    # nothing it invented can reach a search URL.
    options: list[str] = []
    if destination is None:
        destination, options, reason = await resolve_with_model(
            body.destination, call_id="tool"
        )
        log_event("tool.model_resolved", call_id="tool",
                  phrase=body.destination, code=destination, reason=reason)

    if destination is None:
        depart = depart or default_depart_date(None, DEFAULT_ORIGIN)
        if options:
            # Two real cities a long way apart. Choosing for them is not help.
            joined = " or ".join(o.split(",")[0] for o in options[:3])
            say = f"{body.destination} has a few options — {joined}. Which one?"
        else:
            say = (f"I couldn't find an airport for {body.destination}. "
                   "Which city should I look at?")
        return SearchResponse(
            say=say, found=False, origin=origin or DEFAULT_ORIGIN,
            destination=body.destination, depart_date=depart, took_seconds=0.0,
        )
    if origin is None:
        origin = DEFAULT_ORIGIN
    depart = depart or default_depart_date(destination, origin)
    if origin == destination:
        return SearchResponse(
            say=f"That's where you're starting from. Where would you like to fly to?",
            found=False, origin=origin, destination=destination,
            depart_date=depart, took_seconds=0.0,
        )

    key = f"{origin}-{destination}-{depart}"
    started = time.monotonic()
    cached = _cache.get(key)
    fresh = cached is not None and (time.monotonic() - _cached_at.get(key, 0)) < CACHE_TTL_S
    if fresh and cached is not None:
        outcome = cached
        log_event("tool.cache_hit", call_id="tool", route=key)
    else:
        try:
            outcome = await asyncio.wait_for(
                run_search(destination, call_id="tool", origin=origin, depart_date=depart),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            return SearchResponse(
                say=f"The flight sites are being slow for {spoken_name(destination)}. "
                    "Want me to try again?",
                found=False, origin=origin, destination=destination,
                depart_date=depart, took_seconds=round(time.monotonic() - started, 1),
            )
        _cache.put(key, outcome)
        _cached_at[key] = time.monotonic()

    options = _shape(outcome)
    return SearchResponse(
        say=to_sentence(outcome),
        found=outcome.ok,
        origin=origin,
        destination=destination,
        depart_date=depart,
        cheapest=options[0] if options else None,
        alternatives=options[1:3],
        took_seconds=round(time.monotonic() - started, 1),
    )


def _shape(outcome: Any) -> list[Option]:
    """Only the cheapest per platform, so the model sees a handful of rows rather
    than hundreds — it cannot read out what it was never given."""
    if not outcome.ok or outcome.cheapest is None:
        return []
    c = outcome.cheapest
    return [Option(carrier=c.carrier, price_inr=c.price, stops=c.stops, platform=c.platform)]
