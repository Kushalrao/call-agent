"""Cross-search record (Watermelon extension) -> spec section 6 FlightResults.

The extension's per-site parsers already emit a normalized flight, but not
identically across sites: MMT returns `departIso`/`durationMin`/`airlines`
directly, while the extension's own `safeFlight()` reads `departTime`/
`durationTotalMinutes`/`segments[]`. Rather than pick one, read both spellings.

What cross-search adds over a single-site search is the thing worth surfacing:
the same itinerary priced on several platforms. So an itinerary is keyed by
(flight numbers, departure) and carries every platform's price, with the cheapest
promoted to the headline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.airlines import code_for as airline_code

from .localtime import clock_time, spoken_time, to_local

# The extension knows about IndiGo, but we deliberately ignore it: dropped from
# scope on 2026-08-21.
PLATFORMS = ("cleartrip", "mmt", "ixigo", "skyscanner")

PLATFORM_LABELS = {
    "cleartrip": "Cleartrip",
    "mmt": "MakeMyTrip",
    "ixigo": "Ixigo",
    "skyscanner": "Skyscanner",
}

# NOTE (2026-08-21): this module deliberately does NOT merge or dedupe across
# platforms. `lib/analysis/dedupe.js` in the Watermelon extension already does
# that — with fingerprinting, a 10-minute departure bucket, and codeshare/alliance
# awareness — and a second implementation here would be a worse copy competing
# with it. Results stay per-site. If merged output is wanted later, call the
# extension's deduper and read its result rather than reimplementing it.


def _first(d: dict[str, Any], *names: str) -> Any:
    for n in names:
        v = d.get(n)
        if v not in (None, "", [], {}):
            return v
    return None


def _as_int(v: Any) -> int | None:
    try:
        if v is None:
            return None
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _option(f: dict[str, Any], origin: str = "", destination: str = "") -> dict[str, Any] | None:
    """One flight -> one spec section 7 option. Reads both field spellings the
    extension's parsers use (MMT emits departIso/durationMin; others go through
    segments/departTime)."""
    depart = _first(f, "departIso", "departTime")
    price = _as_int(_first(f, "minPrice", "price", "fare", "totalFare"))

    numbers = _first(f, "flightNumbers", "flightNos")
    if not (isinstance(numbers, list) and numbers):
        numbers = []
        for seg in f.get("segments") or []:
            if isinstance(seg, dict):
                joined = f"{seg.get('airlineCode') or ''}{seg.get('flightNumber') or ''}".strip()
                if joined:
                    numbers.append(joined)

    # No price or no departure time means it cannot be shown or compared.
    # Dropping it is correct; inventing a value is not.
    if not depart or price is None or not numbers:
        return None

    carriers = _first(f, "airlines", "airlineNames")
    if not (isinstance(carriers, list) and carriers):
        carriers = []
        for seg in f.get("segments") or []:
            if isinstance(seg, dict):
                name = seg.get("airlineName") or seg.get("airlineCode")
                if name and name not in carriers:
                    carriers.append(str(name))

    # Times are normalised to local-at-the-airport here, once, rather than in
    # each consumer. Cleartrip publishes local+offset and Ixigo publishes UTC, so
    # reading the wall clock straight out of the string — which is what the iOS
    # card does — showed Ixigo departures hours early. See flight_bridge/localtime.
    arrive_raw = str(_first(f, "arriveIso", "arriveTime") or "")
    depart_local = to_local(str(depart), origin)
    arrive_local = to_local(arrive_raw, destination) if arrive_raw else ""

    return {
        "carrier": ", ".join(str(c) for c in carriers) if carriers else "\u2014",
        "flight": " / ".join(str(n).replace(" ", "").upper() for n in numbers),
        "depart": depart_local,
        "arrive": arrive_local,
        # Pre-formatted for speech, so a voice consumer never has to parse or
        # guess a timezone: "8:10 am".
        "depart_spoken": spoken_time(str(depart), origin),
        "arrive_spoken": spoken_time(arrive_raw, destination) if arrive_raw else None,
        # 24-hour for the screen; see clock_time for why the two differ.
        # For the logo. Derived here so the code and the carrier name always come
        # from the same row.
        "airline_code": airline_code(
            ", ".join(str(c) for c in carriers) if carriers else None,
            " / ".join(str(n) for n in numbers) if numbers else None,
        ),
        "depart_clock": clock_time(str(depart), origin),
        "arrive_clock": clock_time(arrive_raw, destination) if arrive_raw else None,
        "stops": _as_int(_first(f, "stops")) or 0,
        "duration_min": _as_int(_first(f, "durationMin", "durationTotalMinutes")),
        "price": {"amount": price, "currency": "INR"},
        "deeplink": None,
    }


def to_widget_payload(record: dict[str, Any], *, limit: int = 3) -> dict[str, Any]:
    """Build the spec section 7 `flight_results` payload, **per platform**.

    Trimming to `limit` per platform happens here, in bridge code, not in the
    model: section 6 requires the LLM never see raw API JSON of any size.
    """
    route = record.get("route") or {}
    sites = record.get("sites") or {}
    platforms: dict[str, Any] = {}

    for key in PLATFORMS:
        site = sites.get(key) or {}
        raw = site.get("flights") or []
        options = [
            o for o in (
                _option(f, route.get("from") or "", route.get("to") or "")
                for f in raw if isinstance(f, dict)
            ) if o
        ]
        options.sort(key=lambda o: o["price"]["amount"])
        platforms[PLATFORM_LABELS[key]] = {
            "status": site.get("status"),
            "error": site.get("error"),
            "raw_flights": len(raw),
            "usable_flights": len(options),
            "cheapest": options[0]["price"]["amount"] if options else None,
            "options": options[:limit],
        }

    return {
        "route": {
            "origin": route.get("from") or "",
            "destination": route.get("to") or "",
        },
        "date_range": [d for d in (route.get("date"), route.get("returnDate")) if d],
        "platforms": platforms,
        "meta": {"cross_search_id": record.get("id")},
    }
