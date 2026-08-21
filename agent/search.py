"""Heard a destination -> real flight search -> one sentence to say aloud.

This is the narrow slice: "hey copilot, flights to Bali". No full `TripContext`
extraction and no model call — `places.resolve()` already finds a known city
inside a phrase, so the destination comes out of the wake utterance for free, in
microseconds, with no tokens spent. The general case (dates, budget, multi-turn
constraints) is spec section 4.3 and comes later.

Decisions baked in here, all confirmed 2026-08-21:

- **Origin defaults to Bangalore.** People almost never say where they are flying
  from. Waiting for it means the loop mostly never fires.
- **Date is today + 10 days** when none is spoken.
- **`wait_for_all=False`** — answer from the first platform to land (~6s) rather
  than the cheapest of three (~16s). Sixteen seconds after the trigger the
  conversation has moved on, and hold-and-expire would drop the voice anyway.
- **Cheapest is a `min` across platforms, never a merge.** That answers the
  question while keeping each site's data separate, which is the standing rule
  for this data.

**The sentence deliberately omits departure times.** Ixigo publishes `Z` (UTC)
while Cleartrip publishes local+offset, so a time read aloud could be ~5.5 hours
wrong — and unlike a card, a misheard time cannot be re-checked. Carrier, price
and stops are all sourced identically across platforms and are safe to speak.
Times go in once that normalization is fixed.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from control_plane.logging_setup import Events, log_event

from .places import is_indian, resolve, spoken_name

DEFAULT_ORIGIN = "BLR"          # Bangalore. Fixed for now.
# How far ahead to look when nobody names a date. Domestic trips get booked much
# closer in than international ones, so a single lead time is wrong for one of
# them: two weeks out is a strange answer for Bombay, and four days out is a
# strange answer for Bali.
DEFAULT_LEAD_DAYS_DOMESTIC = 4
DEFAULT_LEAD_DAYS_INTERNATIONAL = 14
SEARCH_TIMEOUT_S = 45.0  # wait_for_all=False lands in ~7s; 45s is generous


# A page the extension's manifest matches, used only to wake its service worker.
WARMUP_URL = "https://www.cleartrip.com/"
WARMUP_SETTLE_S = 6.0


def _warm_browser() -> None:
    """Start Chrome *and* wake the extension. Blocking; run it in an executor.

    Starting Chrome is not enough. The extension is MV3, so its service worker
    only spins up once a page its manifest matches has loaded — and a
    freshly-started browser has loaded nothing. Asking it to build a search URL
    in that state gets no answer, and the search then times out 45 seconds later
    with no indication that the extension was simply asleep. This is the single
    most confusing failure in the whole flight path, so the warm-up is explicit.
    """
    import json as _json
    import urllib.request

    from flight_scout.capture import chrome_alive, ensure_chrome
    from flight_scout.watermelon import CHROME_PORT, PROFILE_DIR

    ensure_chrome(port=CHROME_PORT, profile_dir=PROFILE_DIR)
    if not chrome_alive(CHROME_PORT):
        return

    try:
        # Any already-open matched tab means the worker is awake; don't pile up
        # tabs on every search.
        with urllib.request.urlopen(
            f"http://127.0.0.1:{CHROME_PORT}/json/list", timeout=10
        ) as r:
            targets = _json.load(r)
        if any("cleartrip.com" in (t.get("url") or "") for t in targets
               if t.get("type") == "page"):
            return

        req = urllib.request.Request(
            f"http://127.0.0.1:{CHROME_PORT}/json/new?{WARMUP_URL}", method="PUT"
        )
        urllib.request.urlopen(req, timeout=15).read()
        time.sleep(WARMUP_SETTLE_S)  # let the worker register its listeners
    except Exception:  # noqa: BLE001
        # Warming is best-effort. If it fails the search will report its own
        # timeout, which is now distinguishable from "no flights found".
        pass


@dataclass
class SearchCache:
    """Results keyed by route, so an answer can be instant.

    This is what makes the ambient path feel immediate rather than slow. A search
    takes 7-12s; if it only starts when someone asks, they wait. Warming it when
    a destination is first *mentioned* means the answer is usually already sitting
    here by the time it is wanted.

    Bounded, because a search is not free in the way a cache normally implies: the
    flight aggregators throttle repeated queries, and one route was blocked during
    testing by re-running it. Each route is fetched once per call.
    """

    entries: dict[str, "SearchOutcome"] = field(default_factory=dict)
    in_flight: set[str] = field(default_factory=set)

    def get(self, key: str) -> "SearchOutcome | None":
        return self.entries.get(key)

    def put(self, key: str, outcome: "SearchOutcome") -> None:
        self.entries[key] = outcome
        self.in_flight.discard(key)

    def claim(self, key: str) -> bool:
        """Reserve a route for fetching. False if cached or already running."""
        if key in self.entries or key in self.in_flight:
            return False
        self.in_flight.add(key)
        return True

    def release(self, key: str) -> None:
        self.in_flight.discard(key)


@dataclass(frozen=True)
class Cheapest:
    platform: str
    carrier: str
    flight: str
    price: int
    stops: int
    currency: str = "INR"
    duration_min: int | None = None
    # Local time at the airport, pre-formatted for speech ("8:10 am"). Safe now
    # that flight_bridge/localtime normalises Ixigo's UTC against the airport's
    # own timezone — before that, speaking a time meant being hours wrong for one
    # platform, which is not something a listener can catch.
    depart_spoken: str | None = None
    arrive_spoken: str | None = None
    # 24-hour, for the card. The screen and the voice want different formats.
    depart_clock: str | None = None
    arrive_clock: str | None = None
    # Two-letter IATA airline code, for the logo. None when unknown, which the
    # card renders as initials rather than as a wrong airline's mark.
    airline_code: str | None = None


@dataclass(frozen=True)
class SearchOutcome:
    origin: str
    destination: str
    depart_date: str
    cheapest: Cheapest | None = None
    platforms_seen: int = 0
    total_options: int = 0
    error: str = ""
    elapsed_s: float = 0.0
    # Every usable flight, cheapest first. The agent used to be handed only the
    # single best fare, so any follow-up — which airlines fly this, is there a
    # direct one, what is the range — had nothing to answer from and it said so.
    options: tuple[Cheapest, ...] = ()

    @property
    def ok(self) -> bool:
        return self.cheapest is not None

    @property
    def airlines(self) -> list[str]:
        """Distinct carriers, cheapest fare first. Order is useful: it answers
        "who flies this" and "who is cheapest" in one list."""
        seen: list[str] = []
        for option in self.options:
            for carrier in option.carrier.split(","):
                carrier = carrier.strip()
                if carrier and carrier != "\u2014" and carrier not in seen:
                    seen.append(carrier)
        return seen

    @property
    def direct(self) -> tuple[Cheapest, ...]:
        return tuple(o for o in self.options if o.stops == 0)

    @property
    def price_range(self) -> tuple[int, int] | None:
        if not self.options:
            return None
        prices = [o.price for o in self.options]
        return (min(prices), max(prices))

    @property
    def fastest(self) -> Cheapest | None:
        timed = [o for o in self.options if o.duration_min]
        return min(timed, key=lambda o: o.duration_min or 0) if timed else None


def default_lead_days(destination: str | None, origin: str = DEFAULT_ORIGIN) -> int:
    """Domestic if both ends are Indian airports, international otherwise.

    Mirrors the extension's own split (`lib/route.js` derives `intl` the same
    way), so a route we call domestic is a route it builds a domestic URL for.
    """
    domestic = is_indian(origin) and is_indian(destination)
    return DEFAULT_LEAD_DAYS_DOMESTIC if domestic else DEFAULT_LEAD_DAYS_INTERNATIONAL


def default_depart_date(
    destination: str | None = None,
    origin: str = DEFAULT_ORIGIN,
    *,
    today: date | None = None,
) -> str:
    """The date to search when the conversation has not named one."""
    lead = default_lead_days(destination, origin)
    return ((today or date.today()) + timedelta(days=lead)).isoformat()


# "to Bali", "for Bali", "into Bali" — the word that marks a destination.
_DEST_MARKERS = (" to ", " for ", " into ", " towards ", " toward ")
_ORIGIN_MARKERS = (" from ", " out of ", " leaving ")


def split_route(text: str) -> tuple[str | None, str | None]:
    """(origin, destination) from a spoken phrase, honouring direction.

    `resolve()` alone scans left to right, so "flights from Bangalore to Bali"
    gave **Bangalore** as the destination and produced a BLR->BLR search. Real
    speech states the origin first far more often than not, so position is not a
    usable signal — the preposition is.

    Splits on the last destination marker so "from Bangalore to Bali" keeps
    "Bali" on the right. Either side may be None.
    """
    padded = f" {' '.join(text.split())} ".lower()

    cut = max((padded.rfind(m), m) for m in _DEST_MARKERS)
    if cut[0] == -1:
        # No direction stated at all — "copilot, Bali flights". One place in the
        # phrase can only be the destination.
        return None, resolve(text)

    left, right = padded[: cut[0]], padded[cut[0] + len(cut[1]) :]

    # "flights to Nice from Bangalore" — the right side holds both, so it has to
    # be cut at the origin marker before resolving. Resolving the whole tail
    # instead found Bangalore and called it the destination.
    tail_origin: str | None = None
    for marker in _ORIGIN_MARKERS:
        idx = right.find(marker)
        if idx != -1:
            tail_origin = right[idx + len(marker) :]
            right = right[:idx]
            break

    destination = resolve(right)
    origin = resolve(left) or (resolve(tail_origin) if tail_origin else None)

    # If the destination side yielded nothing but the tail did, the phrasing was
    # reversed enough that the tail is the real destination.
    if destination is None and tail_origin:
        destination = resolve(tail_origin)
        if destination is not None and origin == destination:
            origin = resolve(left)

    if destination is None:
        # "to" was in the sentence but not before a place we know.
        destination = resolve(text)
        if destination is not None and destination == origin:
            destination = None
    return origin, destination


def destination_from(text: str) -> str | None:
    """The destination out of a spoken phrase, with no model call.

    Returns an IATA code only for cities in the curated table (`places.py`), so
    an unheard-of place yields None and the agent stays quiet rather than
    searching somewhere plausible-but-wrong.
    """
    _, destination = split_route(text)
    return destination


def origin_from(text: str, *, default: str = DEFAULT_ORIGIN) -> str:
    """The stated origin, or the default. People rarely say it."""
    origin, destination = split_route(text)
    if origin is not None and origin != destination:
        return origin
    return default


def collect_options(payload: dict[str, Any]) -> tuple[list[Cheapest], int, int]:
    """Every usable flight across platforms, cheapest first.

    Still a min rather than a merge — each platform's rows are kept as its own,
    which is the standing rule for this data. Nothing is deduplicated across
    sites, so the same flight can appear twice at two prices, and that is
    correct: they really are two different offers.
    """
    collected: list[Cheapest] = []
    platforms_seen = 0

    for label, site in (payload.get("platforms") or {}).items():
        options = site.get("options") or []
        if options:
            platforms_seen += 1
        for option in options:
            amount = (option.get("price") or {}).get("amount")
            if amount is None:
                continue
            collected.append(Cheapest(
                platform=label,
                carrier=option.get("carrier") or "\u2014",
                flight=option.get("flight") or "",
                price=int(amount),
                stops=int(option.get("stops") or 0),
                currency=(option.get("price") or {}).get("currency") or "INR",
                duration_min=option.get("duration_min"),
                depart_spoken=option.get("depart_spoken"),
                arrive_spoken=option.get("arrive_spoken"),
                depart_clock=option.get("depart_clock"),
                arrive_clock=option.get("arrive_clock"),
                airline_code=option.get("airline_code"),
            ))

    collected.sort(key=lambda o: o.price)
    return collected, platforms_seen, len(collected)


def pick_cheapest(payload: dict[str, Any]) -> tuple[Cheapest | None, int, int]:
    """Kept for callers that only want the single best fare."""
    options, platforms_seen, total = collect_options(payload)
    return (options[0] if options else None), platforms_seen, total


def _unused_pick(payload: dict[str, Any]) -> tuple[Cheapest | None, int, int]:
    best: Cheapest | None = None
    platforms_seen = 0
    total = 0
    for label, site in (payload.get("platforms") or {}).items():
        options = site.get("options") or []
        if options:
            platforms_seen += 1
        total += len(options)
        for option in options:
            amount = (option.get("price") or {}).get("amount")
            if amount is None:
                continue
            if best is None or amount < best.price:
                best = Cheapest(
                    platform=label,
                    carrier=option.get("carrier") or "—",
                    flight=option.get("flight") or "",
                    price=int(amount),
                    stops=int(option.get("stops") or 0),
                    currency=(option.get("price") or {}).get("currency") or "INR",
                )
    return best, platforms_seen, total


def to_acknowledgement(destination: str) -> str:
    """What the agent says the instant it is addressed, before it knows anything.

    Exists purely to cover latency. The measured gap between a request and an
    answer is ~11s of which the search is ~10s, and eleven seconds of silence
    after someone asks a direct question reads as "it didn't hear me" — people
    repeat themselves, which then reads as a second request.

    Short on purpose: it is spoken on every request, the free ElevenLabs tier is
    about 10k characters, and a long preamble delays nothing but still steals the
    floor from two people mid-conversation.
    """
    where = spoken_name(destination) or destination
    return f"Checking {where} flights."


def to_sentence(outcome: SearchOutcome) -> str:
    """One sentence, because that is what a call tolerates.

    No departure time — see the module docstring. Nothing here is generated: every
    value comes from the search response.
    """
    where = spoken_name(outcome.destination) or outcome.destination
    if not outcome.ok:
        # Two different failures, and they should not sound the same. "Couldn't
        # search" is a problem on our side; "no flights" is a fact about the world.
        if outcome.error == "same_city":
            return f"You're already in {where} — where did you want to fly to?"
        if outcome.error:
            return f"I couldn't search for flights to {where} just now."
        return f"I didn't find any flights to {where}."

    c = outcome.cheapest
    assert c is not None
    price = f"{c.price:,}".replace(",", ",")
    stops = (
        "direct" if c.stops == 0
        else "one stop" if c.stops == 1
        else f"{c.stops} stops"
    )
    return (
        f"Cheapest to {where} is {c.carrier} at "
        f"{'₹' if c.currency == 'INR' else ''}{price}, {stops}, on {c.platform}."
    )


async def run_search(
    destination: str,
    *,
    call_id: str,
    origin: str = DEFAULT_ORIGIN,
    depart_date: str | None = None,
) -> SearchOutcome:
    """Drive the local Chrome + Watermelon search. Never raises.

    Imported lazily: the flight stack pulls in websockets and Chrome plumbing that
    a worker with no search to do should not pay for at boot.
    """
    depart_date = depart_date or default_depart_date(destination, origin)
    started = time.monotonic()

    # "flights to Bangalore" resolves BLR as the destination, and the origin
    # default is also BLR — which builds a from=BLR&to=BLR URL. Guarded at the
    # single choke point rather than in each caller, because there is no sensible
    # way to search a route to where you already are.
    if origin == destination:
        log_event("search.refused", level="warn", call_id=call_id,
                  origin=origin, destination=destination, reason="same_city")
        return SearchOutcome(origin, destination, depart_date,
                             error="same_city", elapsed_s=0.0)
    log_event("search.started", call_id=call_id, origin=origin,
              destination=destination, depart_date=depart_date)

    try:
        from flight_bridge.normalize import to_widget_payload
        from flight_scout.watermelon import EXTENSION_DIR, cross_search

        # Passed explicitly rather than read from os.environ: .env is loaded by
        # config.py and nowhere else, so flight_scout's env-var default never
        # sees it. Relying on that is what made a missing extension look like an
        # empty result set.
        from control_plane.config import get_settings
        configured = get_settings().watermelon_extension_dir
        extension_dir = Path(configured) if configured else EXTENSION_DIR

        # ensure_chrome inside cross_search is a blocking subprocess launch. In
        # the worker that blocks the whole job's event loop, which stalls STT for
        # every participant — so it is warmed off-loop before the search starts.
        loop = asyncio.get_running_loop()
        log_event("search.warming_browser", call_id=call_id)
        await loop.run_in_executor(None, _warm_browser)
        log_event("search.browser_ready", call_id=call_id)

        result = await asyncio.wait_for(
            cross_search(origin, destination, depart_date,
                         wait_for_all=False, extension_dir=extension_dir),
            timeout=SEARCH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - started
        log_event("search.failed", level="warn", call_id=call_id,
                  reason="timeout", elapsed_s=round(elapsed, 1))
        return SearchOutcome(origin, destination, depart_date,
                             error="timeout", elapsed_s=elapsed)
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - started
        log_event("search.failed", level="error", call_id=call_id,
                  error=str(exc), elapsed_s=round(elapsed, 1))
        return SearchOutcome(origin, destination, depart_date,
                             error=str(exc), elapsed_s=elapsed)

    # Distinguish "the search ran and found nothing" from "the search never
    # ran". A live run reported 0 options in 0.0s because the extension path was
    # wrong, and the agent said "I couldn't find flights to Bali" — technically
    # true, and it hid a misconfiguration completely.
    if not getattr(result, "ok", False):
        elapsed = time.monotonic() - started
        detail = getattr(result, "error", None) or "unknown"
        log_event("search.failed", level="error", call_id=call_id,
                  reason=getattr(result, "error_kind", None) or "not_ok",
                  error=detail, elapsed_s=round(elapsed, 1))
        return SearchOutcome(origin, destination, depart_date,
                             error=detail, elapsed_s=elapsed)

    record = getattr(result, "record", None) or {}
    # Twelve per platform, not the widget's three. A phone card shows three rows;
    # a voice agent has to answer "which airlines fly this", "is there a direct
    # one", "what is the range" — and it can only answer from what it was given.
    payload = to_widget_payload(record, limit=12) if record else {}
    all_options, platforms_seen, total = collect_options(payload)
    cheapest = all_options[0] if all_options else None
    elapsed = time.monotonic() - started

    log_event(
        "search.finished", call_id=call_id, origin=origin, destination=destination,
        platforms_with_results=platforms_seen, options=total,
        cheapest=cheapest.price if cheapest else None,
        cheapest_platform=cheapest.platform if cheapest else None,
        elapsed_s=round(elapsed, 1),
    )
    return SearchOutcome(origin, destination, depart_date, cheapest,
                         platforms_seen, total, elapsed_s=elapsed,
                         # Bounded: enough to answer follow-ups, not so much that
                         # the model starts reading out fare codes.
                         options=tuple(all_options[:24]))
