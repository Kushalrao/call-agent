"""Judgement about a route, to sit alongside the facts about it.

The search returns what exists: carriers, prices, stops, times. It cannot say
which airline is actually good on this route, whether the nonstop is worth two
thousand rupees more, or that the cheapest low-cost fare stops being cheapest once
a checked bag is added. Those are the questions people ask straight after seeing
prices, and they are judgement rather than data.

Three rules make this safe to add.

**It runs concurrently with the search, never after.** The search takes 5-12s and
this takes 1-2s, so asked at the same moment it costs nothing. Asked afterwards it
would add a second or two to every answer, which for a voice product is the whole
budget.

**The fields match the questions.** "Best airline", "nonstop or not" and "anything
to watch out for" are separate fields rather than one blob of prose, so the agent
can answer the question asked instead of reading a paragraph and hoping the
relevant sentence is in it.

**It is opinion, and stays labelled as opinion.** Every number the agent speaks
about a flight comes from the search. Nothing here may introduce a price, a time
or a flight number. The distinction the whole product rests on is that facts come
from tools — this is the first thing allowed to be a view, so it has to be
unmistakably a view.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

import anthropic

from control_plane.config import get_settings
from control_plane.logging_setup import log_event

from .budget import Budget, BudgetExceeded

# Judgement rather than classification, and it runs off the critical path, so it
# is worth a better model than the trigger layer uses.
MODEL = "claude-sonnet-5"
# Off the critical path (see flight_api: the first answer never waits on this),
# so it can afford real thinking time. At 8s it was timing out on Sonnet.
TIMEOUT_S = 25.0
# Generous, because this model thinks before it answers and the thinking shares
# the budget. At 700 the JSON was cut off mid-sentence and logged as
# "unparseable" — a truncated response looks exactly like a broken one unless
# stop_reason is checked, which is why it is logged below.
MAX_TOKENS = 2500

SCHEMA = {
    "type": "object",
    "properties": {
        "best_airline": {
            "type": "string",
            "description": "Which airline on this route, and why, in one sentence.",
        },
        "stops_advice": {
            "type": "string",
            "description": "Nonstop or accept a stop, for this route, in one sentence.",
        },
        "recommendation": {
            "type": "string",
            "description": "Which of the listed flights to actually book, and why.",
        },
        "notes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Up to three route-specific things worth knowing.",
        },
        "watch_out": {
            "type": ["string", "null"],
            "description": "The one thing most likely to catch them out, or null.",
        },
    },
    "required": ["best_airline", "stops_advice", "recommendation", "notes"],
    "additionalProperties": False,
}

SYSTEM = """You advise on a flight search that has already run. You are given the real
options that came back. Your job is the judgement the data cannot supply.

This is read out loud, one line at a time, so every string must be a short
sentence a person would actually say. No markdown, no lists inside strings, no
numbered points.

best_airline: which carrier on the list is the one to fly on this route, and why —
punctuality, seat and legroom, baggage policy, food, how they handle a missed
connection. If two are equally good say so. If the only difference is price, say
that instead of inventing a distinction.

stops_advice: on this specific route, whether to pay for the nonstop or take the
connection. Weigh the actual numbers you were given: an extra two thousand rupees
to save five hours is usually worth it, five hundred rupees to save forty minutes
usually is not. Say which way you would go.

recommendation: which flight on the list you would book, and the reason in the
same breath. Refer to flights by airline, and quote a price only if it is on the
list.

notes: up to three things specific to *this route* a traveller would want and
would not guess. A layover airport that is genuinely unpleasant or genuinely
good. A carrier whose checked bag is not included, so the cheap fare is not the
cheap fare. Transit visa rules for the stopover country. A terminal change that
eats an hour. Skip anything generic — "book early" and "prices vary" are worth
nothing.

watch_out: the single thing most likely to catch this person out, or null if
nothing stands out.

Hard rules:
- Never invent a price, a time, a flight number, or an airline that is not on the
  list. Everything factual about these flights came from a live search; you are
  adding judgement, not data.
- If you are unsure about a route detail, leave it out. A confident wrong claim
  about a transit visa is worse than silence.
- Do not repeat the prices back. The person has already been told them."""


@dataclass(frozen=True)
class RouteAdvice:
    best_airline: str = ""
    stops_advice: str = ""
    recommendation: str = ""
    notes: tuple[str, ...] = ()
    watch_out: str | None = None
    latency_ms: float = 0.0

    @property
    def empty(self) -> bool:
        return not (self.best_airline or self.stops_advice or self.recommendation)


async def advise(
    outcome: object, *, call_id: str = "advice", budget: Budget | None = None
) -> RouteAdvice:
    """Never raises, never blocks a search.

    Advice sits on top of a working answer: if it fails the agent still has the
    fare and the route, and simply has no opinion to offer. That is a much better
    failure than a slow or broken search.
    """
    settings = get_settings()
    options = getattr(outcome, "options", ()) or ()
    if not settings.anthropic_api_key or not options:
        return RouteAdvice()
    if budget is not None:
        if budget.offline:
            return RouteAdvice()
        try:
            budget.check(estimated_usd=0.012)
        except BudgetExceeded as exc:
            log_event("advice.skipped", call_id=call_id, reason=str(exc))
            return RouteAdvice()

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    started = time.perf_counter()
    try:
        response = await asyncio.wait_for(
            client.messages.create(
                model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM,
                messages=[{"role": "user", "content": render_options(outcome)}],
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            ),
            timeout=TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        log_event("advice.failed", level="warn", call_id=call_id, error=str(exc)[:140])
        return RouteAdvice()

    ms = round((time.perf_counter() - started) * 1000, 1)
    if budget is not None:
        usage = response.usage
        budget.record(MODEL, stage="advice", latency_ms=ms,
                      input_tokens=usage.input_tokens,
                      output_tokens=usage.output_tokens)

    advice = parse_advice(response, latency_ms=ms)
    if advice.empty:
        # stop_reason is the difference between "the model produced nonsense" and
        # "we cut it off mid-sentence", which are unrelated problems.
        log_event("advice.unparseable", level="warn", call_id=call_id,
                  stop_reason=getattr(response, "stop_reason", None),
                  output_tokens=getattr(response.usage, "output_tokens", None))
    else:
        log_event("advice.ready", call_id=call_id, latency_ms=ms,
                  notes=len(advice.notes), has_watch_out=advice.watch_out is not None)
    return advice


def _text_of(response: object) -> str | None:
    """The response's text, wherever it sits in the content list.

    `content[0].text` is wrong on a thinking model: the first block is the
    reasoning, not the answer, so this parsed as empty for ten seconds and logged
    "unparseable" while the call had actually succeeded. Scanning for the text
    block is correct regardless of which model is configured.
    """
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) in (None, "text"):
            text = getattr(block, "text", None)
            if text:
                return text
    return None


def parse_advice(response: object, *, latency_ms: float = 0.0) -> RouteAdvice:
    """Defensive: advice failing must never break a search that worked."""
    try:
        raw = _text_of(response)
        if not raw:
            return RouteAdvice()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return RouteAdvice()
        watch = data.get("watch_out")
        return RouteAdvice(
            best_airline=str(data.get("best_airline") or "").strip(),
            stops_advice=str(data.get("stops_advice") or "").strip(),
            recommendation=str(data.get("recommendation") or "").strip(),
            notes=tuple(
                str(n).strip() for n in (data.get("notes") or [])[:3] if str(n).strip()
            ),
            watch_out=(str(watch).strip() or None) if watch else None,
            latency_ms=latency_ms,
        )
    except (AttributeError, IndexError, ValueError, TypeError, json.JSONDecodeError):
        return RouteAdvice()


def render_options(outcome: object) -> str:
    """The flights as a compact table. Only what came back from the search."""
    from .places import spoken_name

    origin = getattr(outcome, "origin", "")
    destination = getattr(outcome, "destination", "")
    lines = [
        f"Route: {spoken_name(origin) or origin} to {spoken_name(destination) or destination}",
        f"Date: {getattr(outcome, 'depart_date', '')}",
        "",
        "Options found (cheapest first):",
    ]
    for option in (getattr(outcome, "options", ()) or ())[:12]:
        duration = f"{option.duration_min}min" if option.duration_min else "duration unknown"
        stops = "nonstop" if option.stops == 0 else f"{option.stops} stop"
        when = f", departs {option.depart_spoken}" if option.depart_spoken else ""
        lines.append(
            f"- {option.carrier}: Rs {option.price:,}, {stops}, {duration}{when} "
            f"(found on {option.platform})"
        )
    return "\n".join(lines)
