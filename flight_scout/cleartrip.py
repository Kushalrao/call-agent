"""Cleartrip: build a searchable URL, and parse the response the page fetches.

Two pure functions, no browser involved. `search_url()` mirrors
`buildSearchUrl('cleartrip', route)` from the Watermelon extension's
`lib/route.js`; `parse()` reads the `/flight/search/v2` body.

Verified against a real 1.9 MB capture (BLR->DEL, 235 cards) on 2026-08-21.

Response linkage, which is not obvious from the payload:

    cards.J1[i]                       one itinerary as displayed
      .summary.flights[]              airline code + flight number per leg
      .summary.firstDeparture/...     ISO times with the airport's own offset
      .subTravelOptionIds[]  ------>  subTravelOptions[id]
                                        .fareIds[]  ------>  fares[fareId]
                                                               .pricing.totalPricing.totalPrice
    metaData.airlineDetail[code]      airline display name

`travelOptionIdsToFareIdsMap` exists but was empty in the live capture — the
usable path is through `subTravelOptions`.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

# Same set lib/route.js uses to decide the `intl` flag. Cleartrip returns
# 400 SEARCH_VALIDATION_FAILED when the flag disagrees with the route, so this
# has to be right rather than guessed.
IN_AIRPORTS = {
    "DEL", "BOM", "BLR", "MAA", "HYD", "CCU", "AMD", "COK", "GOI", "GOX",
    "AGR", "AGX", "AJL", "AKD", "ATQ", "BBI", "BDQ", "BEK", "BEP", "BHJ", "BHO",
    "BHR", "BHU", "BKB", "BUP", "CCJ", "CDP", "CJB", "DBR", "DED", "DHM", "DIB",
    "DIU", "DMU", "GAU", "GAY", "GUX", "HBX", "HJR", "HSS", "IDR", "IMF", "IXA",
    "IXB", "IXC", "IXD", "IXE", "IXG", "IXJ", "IXL", "IXM", "IXR", "IXS", "IXU",
    "IXY", "IXZ", "JAI", "JDH", "JGA", "JLR", "JRG", "JSA", "KLH", "KNU", "KUU",
    "LDH", "LKO", "MYQ", "NAG", "NDC", "NMB", "PAB", "PAT", "PNQ", "PYG", "RAJ",
    "RJA", "RPR", "SAG", "SHL", "STV", "SXR", "TCR", "TEZ", "TIR", "TRV", "TRZ",
    "UDR", "VGA", "VNS", "VTZ", "VDY", "PYB", "REW", "SLV", "GBI", "HRI", "PGH",
    "RJI", "OMN",
}

CABIN_LABELS = {
    "ECONOMY": "Economy",
    "PREMIUM_ECONOMY": "Premium Economy",
    "BUSINESS": "Business",
    "FIRST": "First",
}

# The XHR the results page fires. Same pattern as the extension's
# FLIGHT_API_PATTERNS.cleartrip.
SEARCH_API_PATTERN = r"/flight/search/v2(\?|$)"


def is_international(origin: str, destination: str) -> bool:
    return not (origin.upper() in IN_AIRPORTS and destination.upper() in IN_AIRPORTS)


def search_url(
    origin: str,
    destination: str,
    depart_date: str,
    *,
    return_date: str | None = None,
    adults: int = 1,
    children: int = 0,
    infants: int = 0,
    cabin: str = "ECONOMY",
) -> str:
    """A directly navigable Cleartrip results URL. No session or referrer needed."""
    origin, destination = origin.upper(), destination.upper()

    def ddmmyyyy(iso: str) -> str:
        y, m, d = iso.split("-")
        return f"{d}/{m}/{y}"

    params = {
        "adults": adults,
        "childs": children,
        "infants": infants,
        "class": CABIN_LABELS.get(cabin.upper(), "Economy"),
        "depart_date": ddmmyyyy(depart_date),
        "from": origin,
        "intl": "y" if is_international(origin, destination) else "n",
        "to": destination,
    }
    if return_date:
        params["return_date"] = ddmmyyyy(return_date)

    return "https://www.cleartrip.com/flights/results?" + urllib.parse.urlencode(params)


# --- parsing ----------------------------------------------------------------


def _duration_min(d: Any) -> int | None:
    if not isinstance(d, dict):
        return None
    hh, mm = d.get("hh"), d.get("mm")
    if hh is None and mm is None:
        return None
    return int(hh or 0) * 60 + int(mm or 0)


def _cheapest_fare(card: dict, sub_options: dict, fares: dict) -> float | None:
    prices: list[float] = []
    for sub_id in card.get("subTravelOptionIds") or []:
        sub = sub_options.get(sub_id) or {}
        for fare_id in sub.get("fareIds") or []:
            fare = fares.get(fare_id) or {}
            total = (
                (fare.get("pricing") or {}).get("totalPricing") or {}
            ).get("totalPrice")
            if isinstance(total, (int, float)) and total > 0:
                prices.append(float(total))
    return min(prices) if prices else None


def parse(
    body: str | dict,
    *,
    expect_destination: str | None = None,
) -> dict[str, Any]:
    """Parse a `/flight/search/v2` body into the shape `flight_bridge.normalize`
    already understands (`departIso`, `flightNumbers`, `minPrice`, ...).

    `expect_destination` filters out nearby-airport results: Cleartrip helpfully
    includes them (a BLR->DEL search returns BLR->HDO cards), which would
    otherwise show up as suspiciously cheap options to a different city.
    """
    import json

    if isinstance(body, str):
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"json parse failed: {exc}", "flights": []}
    else:
        data = body

    cards = ((data.get("cards") or {}).get("J1")) or []
    if not isinstance(cards, list) or not cards:
        return {
            "ok": False,
            "error": "no cards.J1",
            "flights": [],
            "keys": list(data.keys())[:15],
        }

    sub_options = data.get("subTravelOptions") or {}
    fares = data.get("fares") or {}
    airline_names = (data.get("metaData") or {}).get("airlineDetail") or {}

    flights: list[dict[str, Any]] = []
    skipped_nearby = 0
    skipped_unpriced = 0

    for card in cards:
        summary = card.get("summary") or {}
        legs = summary.get("flights") or []
        first = (summary.get("firstDeparture") or {}).get("airport") or {}
        last = (summary.get("lastArrival") or {}).get("airport") or {}

        destination = last.get("code") or ""
        if expect_destination and destination.upper() != expect_destination.upper():
            skipped_nearby += 1
            continue

        price = _cheapest_fare(card, sub_options, fares)
        if price is None:
            skipped_unpriced += 1
            continue

        codes = [str(l.get("airlineCode") or "") for l in legs if l.get("airlineCode")]
        numbers = [
            f"{l.get('airlineCode') or ''}{l.get('flightNumber') or ''}".strip()
            for l in legs
            if l.get("flightNumber")
        ]
        carriers: list[str] = []
        for code in codes:
            detail = airline_names.get(code)
            name = detail.get("name") if isinstance(detail, dict) else detail
            label = str(name or code)
            if label not in carriers:
                carriers.append(label)

        flights.append(
            {
                "travelOptionId": card.get("travelOptionId") or "",
                "airlines": carriers,
                "airlineCodes": codes,
                "flightNumbers": numbers,
                "origin": first.get("code") or "",
                "destination": destination,
                "departIso": first.get("time") or "",
                "arriveIso": last.get("time") or "",
                "durationMin": _duration_min(summary.get("totalDuration")),
                "stops": int(summary.get("stops") or 0),
                "minPrice": price,
                "layovers": [
                    {
                        "airport": lay.get("airport"),
                        "durationMin": _duration_min(lay.get("totalDuration")),
                    }
                    for lay in (card.get("layover") or [])
                ],
            }
        )

    flights.sort(key=lambda f: f["minPrice"])
    return {
        "ok": bool(flights),
        "error": None if flights else "no priced flights",
        "flights": flights,
        "diagnostics": {
            "cards": len(cards),
            "priced": len(flights),
            "skipped_nearby_airport": skipped_nearby,
            "skipped_unpriced": skipped_unpriced,
        },
    }
