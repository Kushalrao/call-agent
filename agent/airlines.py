"""Airline IATA codes, so a row can show the real carrier's logo.

Logos are fetched by two-letter airline code, and the search returns carrier
*names* ("Air India Express", "Thai Air Asia"). The flight number usually carries
the code — "6E1471" is IndiGo — but not every platform emits one, so names are
mapped too.

Deliberately returns None rather than a guess. A logo is an assertion about who
operates the flight, and the wrong airline's mark next to a real fare is worse
than no mark at all: initials are obviously a placeholder, whereas an Emirates
logo on a SpiceJet flight is a lie the user has no way to catch.
"""

from __future__ import annotations

import re

# Carriers this product actually sees, plus the majors on routes out of India.
# Extend when a real search shows a name that falls through to initials.
NAME_TO_CODE: dict[str, str] = {
    "indigo": "6E",
    "air india": "AI",
    "air india express": "IX",
    "air-india express": "IX",
    "akasa": "QP",
    "akasa air": "QP",
    "spicejet": "SG",
    "vistara": "UK",
    "alliance air": "9I",
    "srilankan": "UL",
    "srilankan airlines": "UL",
    "emirates": "EK",
    "etihad": "EY",
    "etihad airways": "EY",
    "qatar airways": "QR",
    "singapore airlines": "SQ",
    "thai airways": "TG",
    "thai airasia": "FD",
    "thai air asia": "FD",
    "airasia": "AK",
    "air asia": "AK",
    "malaysia airlines": "MH",
    "batik air": "ID",
    "scoot": "TR",
    "garuda": "GA",
    "garuda indonesia": "GA",
    "vietjet": "VJ",
    "vietjet air": "VJ",
    "vietnam airlines": "VN",
    "cathay pacific": "CX",
    "gulf air": "GF",
    "oman air": "WY",
    "salamair": "OV",
    "flydubai": "FZ",
    "air arabia": "G9",
    "kuwait airways": "KU",
    "saudia": "SV",
    "turkish airlines": "TK",
    "nepal airlines": "RA",
    "himalaya airlines": "H9",
    "drukair": "KB",
    "bangkok airways": "PG",
    "bhutan airlines": "B3",
    "maldivian": "Q2",
    "lufthansa": "LH",
    "british airways": "BA",
    "air france": "AF",
    "klm": "KL",
    "swiss": "LX",
    "virgin atlantic": "VS",
    "japan airlines": "JL",
    "ana": "NH",
    "korean air": "KE",
    "china southern": "CZ",
    "cebu pacific": "5J",
    "philippine airlines": "PR",
    "ethiopian airlines": "ET",
    "kenya airways": "KQ",
    "air mauritius": "MK",
    "fly91": "IC",
    "star air": "S5",
}

# "6E1471", "AI2/TG3", "QP 1105" — exactly two characters, then the number.
# An earlier pattern allowed a third and turned 6E1471 into "6E1", which is not an
# airline. Airline IATA codes are two characters; the rare three-character ones
# are covered by the name map instead.
_FLIGHT_CODE = re.compile(r"^\s*([0-9A-Z]{2})\s*\d", re.IGNORECASE)


def from_flight_number(flight: str | None) -> str | None:
    """The airline code out of a flight number, if there is one."""
    if not flight:
        return None
    first = flight.split("/")[0].strip()
    match = _FLIGHT_CODE.match(first)
    if not match:
        return None
    code = match.group(1).upper()
    return code if 2 <= len(code) <= 3 else None


def from_name(carrier: str | None) -> str | None:
    """The airline code from a carrier name."""
    if not carrier:
        return None
    # A codeshare arrives as "Air India, Thai Airways"; the first is the operator
    # as far as a single logo can represent it.
    primary = carrier.split(",")[0].strip().lower()
    primary = re.sub(r"\s+", " ", primary)
    if primary in NAME_TO_CODE:
        return NAME_TO_CODE[primary]
    # "Air India Express Connect" -> longest known prefix wins, so a suffixed
    # brand does not fall back to its parent airline's logo by accident.
    best: str | None = None
    best_len = 0
    for name, code in NAME_TO_CODE.items():
        if primary.startswith(name) and len(name) > best_len:
            best, best_len = code, len(name)
    return best


def code_for(carrier: str | None, flight: str | None = None) -> str | None:
    """Best available code, flight number first.

    The number is the stronger signal: it comes from the platform's own data
    rather than from matching a display name we may render differently.
    """
    return from_flight_number(flight) or from_name(carrier)
