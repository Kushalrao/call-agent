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
    # Minutes. Deliberately no departure time: Ixigo publishes times in UTC while
    # Cleartrip publishes local time with an offset, so any absolute time here
    # would be wrong by hours for one of them. Duration is directly comparable.
    duration_min: int | None = None


class Summary(BaseModel):
    """Answers to the questions people actually ask next.

    Precomputed rather than left to the model. Asked "which is cheapest" from a
    list of twelve rows a model will usually get it right and occasionally not,
    and there is no reason to leave arithmetic to inference.
    """

    total_found: int
    price_low_inr: int | None = None
    price_high_inr: int | None = None
    airlines: list[str] = []
    direct_available: bool = False
    direct_count: int = 0
    cheapest_direct: Option | None = None
    fastest: Option | None = None
    platforms_searched: int = 0


class SearchResponse(BaseModel):
    """`say` is the answer to read out. Everything else is there so the agent can
    be cross-questioned — which airlines fly this, is there a direct one, what is
    the range — without searching again."""

    say: str
    found: bool
    origin: str
    destination: str
    depart_date: str
    cheapest: Option | None = None
    summary: Summary | None = None
    options: list[Option] = []
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
            say = f"{body.destination} — {_spoken_list(options[:3])}. Which one?"
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

    rows = [_row(o) for o in outcome.options]
    low_high = outcome.price_range
    direct = outcome.direct
    return SearchResponse(
        say=to_sentence(outcome),
        found=outcome.ok,
        origin=origin,
        destination=destination,
        depart_date=depart,
        cheapest=_row(outcome.cheapest) if outcome.cheapest else None,
        summary=Summary(
            total_found=outcome.total_options,
            price_low_inr=low_high[0] if low_high else None,
            price_high_inr=low_high[1] if low_high else None,
            airlines=outcome.airlines,
            direct_available=bool(direct),
            direct_count=len(direct),
            cheapest_direct=_row(direct[0]) if direct else None,
            fastest=_row(outcome.fastest) if outcome.fastest else None,
            platforms_searched=outcome.platforms_seen,
        ) if outcome.ok else None,
        # Capped. Twelve rows is enough to be cross-questioned about and few
        # enough that the model does not start reading out fare codes.
        options=rows[:12],
        took_seconds=round(time.monotonic() - started, 1),
    )


def _spoken_list(codes: list[str]) -> str:
    """Airport codes as a list a person would actually say.

    Two things this gets right that the obvious version does not. `" or ".join`
    produces "Bangkok or Phuket or Chiang Mai", which nobody says; and two items
    take no comma at all — "Tokyo, or Osaka" reads as a stumble.

    Names come from spoken_name, so the curated ones win: the dataset calls Bali
    "Denpasar-Bali Island" and Kochi "Cochin", and neither is a word anyone uses.
    """
    cities = [spoken_name(c) for c in codes]
    cities = [c for c in cities if c]
    if not cities:
        return ""
    if len(cities) == 1:
        return cities[0]
    if len(cities) == 2:
        return f"{cities[0]} or {cities[1]}"
    return ", ".join(cities[:-1]) + f", or {cities[-1]}"


def _row(option: Any) -> Option:
    return Option(
        carrier=option.carrier,
        price_inr=option.price,
        stops=option.stops,
        platform=option.platform,
        duration_min=option.duration_min,
    )
