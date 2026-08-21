"""One lovely, true thing about a city, to fill the wait for flight prices.

A search takes eight to twelve seconds. The agent already says "checking Bangkok
flights" and then goes quiet, and eight seconds of silence after that is the
longest part of the whole interaction. Something worth hearing belongs there.

Why this is a lookup rather than a line in the prompt: asked to improvise about a
city, a model will occasionally give Delhi beaches or put Hoi An in Thailand. Said
out loud to someone planning a trip, that is worse than silence — they cannot
check it, and it makes everything else the agent says less believable. So the
sentence is generated once, deliberately, with a prompt that is mostly about what
not to claim, and then cached: the same city gets the same note for the life of
the process.

Kept to one sentence because it is spoken over a wait, not delivered as a fact
sheet. It should sound like a friend who has been there, not a guidebook.
"""

from __future__ import annotations

import asyncio
import json
import time

import anthropic

from control_plane.config import get_settings
from control_plane.logging_setup import log_event

from .airports import airport_name, describe
from .budget import Budget, BudgetExceeded
from .places import spoken_name

MODEL = "claude-haiku-4-5"
# It has to come back before the search does, and it is spoken first. Anything
# slower than this is worse than no note at all.
TIMEOUT_S = 3.0
MAX_TOKENS = 200

SCHEMA = {
    "type": "object",
    "properties": {
        "note": {
            "type": "string",
            "description": "One spoken sentence about the city. Empty if unsure.",
        }
    },
    "required": ["note"],
    "additionalProperties": False,
}

SYSTEM = """You write one sentence about a city, to be spoken aloud to someone who is
about to look at flights there.

It should be the kind of thing a friend who has actually been would say — a
specific, true, slightly surprising detail that makes the place feel real. Not a
guidebook opening, not a superlative, not a list.

What makes a good one: name somewhere specific in the city and say what people
actually do there. The detail should be small enough that only someone who had
been would mention it.

What makes a bad one: "vibrant city with a rich culture" says nothing. "Famous for
its beaches" is something they already know. "Best visited in winter" is about the
trip, not the place.

An illustration of the *shape*, about a city you will not be asked for — "Porto's
port houses are all across the river in Gaia, so the city you drink in is not the
city you sleep in." Do not reuse this sentence or its subject; it is here to show
the register, not to be borrowed. Write about the city you were given.

Hard rules:
- One sentence. Twenty five words at most. It is spoken, so no lists, no dashes
  standing in for clauses, no markdown.
- Only what you are sure of. If you do not have a specific true detail for this
  city, return an empty string — silence is better than a confident invention
  about somewhere the person is about to fly.
- Never mention flights, prices, airlines or the weather forecast.
- Do not open with the city name as a label. Write a sentence, not a heading."""


class CityNotes:
    """Per-city notes, generated once and kept.

    Cached for the life of the process because a fact about a city does not change
    between two conversations, and paying a model to re-derive the same sentence
    would be waste that the caller feels as latency.
    """

    def __init__(self) -> None:
        self._notes: dict[str, str] = {}
        self._inflight: dict[str, asyncio.Task[str]] = {}

    def cached(self, code: str) -> str | None:
        return self._notes.get(code.upper()) if code else None

    async def note_for(
        self, code: str, *, call_id: str = "citynote", budget: Budget | None = None
    ) -> str:
        """One sentence, or "" when we have nothing worth saying.

        Never raises. A missing note means the agent says its normal "checking
        flights" line and nothing is lost.
        """
        if not code:
            return ""
        key = code.upper()
        if key in self._notes:
            return self._notes[key]

        # Two callers asking at once should produce one model call, not two.
        existing = self._inflight.get(key)
        if existing is not None:
            try:
                return await asyncio.shield(existing)
            except Exception:  # noqa: BLE001
                return ""

        task = asyncio.create_task(self._generate(key, call_id=call_id, budget=budget))
        self._inflight[key] = task
        try:
            return await task
        finally:
            self._inflight.pop(key, None)

    async def _generate(
        self, code: str, *, call_id: str, budget: Budget | None
    ) -> str:
        settings = get_settings()
        if not settings.anthropic_api_key:
            return ""
        if budget is not None:
            if budget.offline:
                return ""
            try:
                budget.check(estimated_usd=0.001)
            except BudgetExceeded:
                return ""

        city = spoken_name(code) or code
        where = describe(code) or city
        # The dataset's city field is sometimes the airport's village rather than
        # the place people mean — ZNZ is filed under "Kiembi Samaki" — so the
        # airport name goes in too, which is where "Zanzibar" actually appears.
        airport = airport_name(code) or ""
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        started = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                client.messages.create(
                    model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM,
                    messages=[{
                        "role": "user",
                        "content": f"{city} ({where})"
                        + (f", airport: {airport}" if airport else ""),
                    }],
                    output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                ),
                timeout=TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001
            log_event("citynote.failed", level="warn", call_id=call_id,
                      city=city, error=str(exc)[:120])
            return ""

        ms = round((time.perf_counter() - started) * 1000, 1)
        if budget is not None:
            usage = response.usage
            budget.record(MODEL, stage="citynote", latency_ms=ms,
                          input_tokens=usage.input_tokens,
                          output_tokens=usage.output_tokens)

        note = parse_note(response)
        # Cached even when empty, so a city we have nothing for is not asked about
        # again every time it comes up.
        self._notes[code] = note
        log_event("citynote.ready", call_id=call_id, city=city,
                  latency_ms=ms, chars=len(note), empty=not note)
        return note


def parse_note(response: object) -> str:
    """Defensive, and enforces the length rule the prompt asks for."""
    try:
        for block in getattr(response, "content", None) or []:
            text = getattr(block, "text", None)
            if not text:
                continue
            data = json.loads(text)
            note = str(data.get("note") or "").strip()
            # A model that ignored "one sentence" gets trimmed rather than
            # trusted: this is spoken over a wait, and a paragraph would still be
            # going when the prices arrive.
            if len(note.split()) > 30:
                return ""
            return note
    except (ValueError, TypeError, json.JSONDecodeError):
        return ""
    return ""


# One per process. The cache is the point.
NOTES = CityNotes()
