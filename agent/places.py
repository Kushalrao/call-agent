"""City names as people say them -> IATA codes the search needs.

The Watermelon extension works entirely in IATA codes (`lib/route.js` builds
every URL from `from`/`to` codes and only keeps an Indian-airport set for
`detectIntl`). There is no name resolver anywhere in it, so this is the one piece
that genuinely has to be new.

**A curated table, deliberately, rather than asking the model for the code.**
Claude knows Bali is DPS — and will also confidently produce a plausible code for
a city it is unsure about. A wrong IATA code does not fail; it silently searches
the wrong city and the agent then reads a real price for the wrong place aloud.
The same discipline as everywhere else in this repo: the agent never states a fact
it did not get from a source it can check.

**Keyed to `vocabulary.py` on purpose.** Those are the exact strings Deepgram is
biased toward, so anything the agent can reliably *hear* is something it can
resolve. A test asserts every origin and destination in the keyterm list has a
code here — otherwise the two lists drift and the failure looks like "the agent
ignored me" rather than "that city is not in the table".
"""

from __future__ import annotations

import re
import unicodedata

from .vocabulary import DESTINATIONS, ORIGINS

# city (as spoken) -> IATA. Aliases share a code; the spoken form is the key.
CITY_TO_IATA: dict[str, str] = {
    # --- Indian origins ---
    "bangalore": "BLR", "bengaluru": "BLR",
    "delhi": "DEL", "new delhi": "DEL",
    "mumbai": "BOM", "bombay": "BOM",
    "hyderabad": "HYD",
    "chennai": "MAA", "madras": "MAA",
    "kolkata": "CCU", "calcutta": "CCU",
    "pune": "PNQ",
    "ahmedabad": "AMD",
    "goa": "GOX", "goa airport": "GOX",
    "kochi": "COK", "cochin": "COK",
    "jaipur": "JAI",
    "srinagar": "SXR",
    "leh": "IXL",
    "manali": "KUU", "bhuntar": "KUU",
    "andaman": "IXZ", "port blair": "IXZ",
    "varanasi": "VNS", "banaras": "VNS",
    "lucknow": "LKO",
    "trivandrum": "TRV", "thiruvananthapuram": "TRV",

    # --- international destinations ---
    "bali": "DPS", "denpasar": "DPS",
    "dubai": "DXB",
    "abu dhabi": "AUH",
    "singapore": "SIN",
    "bangkok": "BKK",
    "phuket": "HKT",
    "kuala lumpur": "KUL",
    "colombo": "CMB",
    "maldives": "MLE", "male": "MLE",
    "kathmandu": "KTM",
    "tokyo": "NRT",
    "seoul": "ICN",
    "hong kong": "HKG",
    "istanbul": "IST",
    "london": "LHR",
    "paris": "CDG",
    "amsterdam": "AMS",
    "zurich": "ZRH",
    "new york": "JFK",
    "tbilisi": "TBS",
    "baku": "GYD",
    "almaty": "ALA",
    "hanoi": "HAN",
    "da nang": "DAD", "danang": "DAD",
    "saigon": "SGN", "ho chi minh": "SGN", "ho chi minh city": "SGN",
}

# Every code the table can emit; used to validate a code the model volunteered.
KNOWN_CODES: frozenset[str] = frozenset(CITY_TO_IATA.values())

_CODE = re.compile(r"^[A-Z]{3}$")


def normalize_place(text: str) -> str:
    """Fold accents, punctuation and spacing so "Bengalūru," matches "bengaluru"."""
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = re.sub(r"[^\w\s]+", " ", folded.lower())
    return re.sub(r"\s+", " ", folded).strip()


def resolve(place: str | None) -> str | None:
    """A spoken place name (or an IATA code) -> a code we are willing to search.

    Returns None rather than guessing. An unresolved city means the agent stays
    quiet about that route, which is the correct failure: silence is recoverable,
    a confident price for the wrong city is not.
    """
    if not place:
        return None
    raw = place.strip()

    # An already-uppercase 3-letter token is a code — but only if we know it.
    if _CODE.match(raw) and raw in KNOWN_CODES:
        return raw

    key = normalize_place(raw)
    if not key:
        return None
    if key in CITY_TO_IATA:
        return CITY_TO_IATA[key]

    # A bare code in other casing ("dps"), still validated against the table.
    if len(key) == 3 and key.upper() in KNOWN_CODES:
        return key.upper()

    # "flying to bali" / "bali indonesia" — find a known city inside the phrase,
    # preferring the longest match so "new york" beats a stray "york".
    words = key.split()
    for size in range(min(3, len(words)), 0, -1):
        for i in range(len(words) - size + 1):
            candidate = " ".join(words[i : i + size])
            if candidate in CITY_TO_IATA:
                return CITY_TO_IATA[candidate]
    return None


def is_indian(code: str | None) -> bool:
    """Mirrors the extension's own domestic/international split (lib/route.js).
    Kept small: it only needs the origins this product actually flies from."""
    return code in {
        "BLR", "DEL", "BOM", "HYD", "MAA", "CCU", "AMD", "GOX", "COK", "JAI",
        "PNQ", "SXR", "IXL", "KUU", "IXZ", "VNS", "LKO", "TRV",
    }


def spoken_name(code: str | None) -> str:
    """A code back to something worth saying aloud. Falls back to the code, which
    is ugly but never wrong."""
    if not code:
        return ""
    for name, mapped in CITY_TO_IATA.items():
        if mapped == code:
            return name.title()
    return code


def missing_codes() -> list[str]:
    """Vocabulary entries with no code. Should always be empty — see the test."""
    return sorted(
        term for term in (*ORIGINS, *DESTINATIONS)
        if normalize_place(term) not in CITY_TO_IATA
    )
