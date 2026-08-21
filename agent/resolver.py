"""Last-resort place resolution, for what a dataset cannot answer.

`places.resolve` handles anything with a name — 7,884 airports, every city and
most airport names. What it cannot handle is language: "Vietnam" is a country,
"the place with the pyramids" is a description, and "somewhere warm in December"
is a wish. A person says those things; a lookup table has nothing to say back.

So a model is asked. The important part is what it is *not* trusted with:

**It never returns an airport code.** It returns city names, which are then looked
up in the dataset exactly like anything the user said directly. A model asked for
"the IATA code for Hanoi" will also confidently produce one for a town it has
never heard of, and a wrong code does not fail — it silently prices the wrong
city and the agent reads that price out loud. Proposing a name that must then be
found in real data removes that failure entirely.

**A country stays a question.** Asked about Vietnam it offers Hanoi and Ho Chi
Minh City rather than picking one, because choosing on someone's behalf between
two cities a thousand kilometres apart is not helpfulness.
"""

from __future__ import annotations

import asyncio
import json
import time

import anthropic

from control_plane.config import get_settings
from control_plane.logging_setup import log_event

from .airports import describe, lookup
from .budget import Budget, BudgetExceeded

MODEL = "claude-haiku-4-5"
TIMEOUT_S = 4.0
MAX_TOKENS = 200

SCHEMA = {
    "type": "object",
    "properties": {
        "cities": {
            "type": "array",
            "items": {"type": "string"},
            "description": "City names, most likely first. Empty if this is not a place.",
        },
        "ambiguous": {
            "type": "boolean",
            "description": "True when the user must choose between the cities.",
        },
    },
    "required": ["cities", "ambiguous"],
    "additionalProperties": False,
}

SYSTEM = """You turn what someone said about where they want to fly into city names.

Return city names only — never airport codes. Something else looks the codes up in
real airport data, so a name that does not exist simply fails, while a code you
guessed would quietly become a real price for the wrong place.

Rules:
- A city, however spelled or mispronounced, returns that one city.
- A country or region returns the cities people actually fly into, biggest first,
  with ambiguous=true. Vietnam is Hanoi and Ho Chi Minh City. Japan is Tokyo and
  Osaka. Do not choose for them.
- A landmark or description returns the nearest city people fly to. The pyramids
  is Cairo. Machu Picchu is Cusco. ambiguous=false.
- An island or region with one obvious airport returns that city, ambiguous=false.
  Bali is Denpasar. Maldives is Male.
- If it is not a place at all, return an empty list.
- Never return more than four cities.

Return only names. No codes, no explanations, no airports."""


async def resolve_with_model(
    phrase: str, *, call_id: str = "resolver", budget: Budget | None = None
) -> tuple[str | None, list[str], str]:
    """(code, candidate descriptions, reason).

    A single code means it resolved. Several candidates mean the caller should
    ask. Neither means we genuinely do not know, which is a fine answer.
    """
    settings = get_settings()
    if not settings.anthropic_api_key or not phrase.strip():
        return None, [], "unavailable"
    if budget is not None:
        if budget.offline:
            return None, [], "offline"
        try:
            budget.check(estimated_usd=0.001)
        except BudgetExceeded as exc:
            return None, [], f"budget:{exc}"

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    started = time.perf_counter()
    try:
        response = await asyncio.wait_for(
            client.messages.create(
                model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM,
                messages=[{"role": "user", "content": phrase}],
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            ),
            timeout=TIMEOUT_S,
        )
    except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
        log_event("resolver.failed", level="warn", call_id=call_id,
                  phrase=phrase, error=str(exc)[:120])
        return None, [], "error"

    ms = round((time.perf_counter() - started) * 1000, 1)
    if budget is not None:
        usage = response.usage
        budget.record(MODEL, stage="resolver", latency_ms=ms,
                      input_tokens=usage.input_tokens,
                      output_tokens=usage.output_tokens)

    try:
        data = json.loads(response.content[0].text)
        suggested = [str(c) for c in (data.get("cities") or [])][:4]
        ambiguous = bool(data.get("ambiguous"))
    except (AttributeError, IndexError, ValueError, TypeError, json.JSONDecodeError):
        return None, [], "unparseable"

    # Every suggestion goes through the same lookup a user's own words would.
    # This is the guarantee: nothing the model invented can reach a search URL.
    resolved: list[tuple[str, str]] = []
    for city in suggested:
        code = lookup(city)
        if code and all(code != existing for existing, _ in resolved):
            resolved.append((code, describe(code) or city))

    log_event("resolver.result", call_id=call_id, phrase=phrase, latency_ms=ms,
              suggested=suggested, resolved=[c for c, _ in resolved],
              ambiguous=ambiguous)

    if not resolved:
        return None, [], "no_match"
    if ambiguous and len(resolved) > 1:
        return None, [name for _, name in resolved], "ambiguous"
    return resolved[0][0], [], "resolved"
