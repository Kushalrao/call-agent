"""Every airport in the world, so the agent stops asking which city you mean.

The curated table in `places.py` covers about forty cities. That was right while
the only consumer was a two-person call about a handful of routes, and wrong the
moment someone said "Hanoi" — the agent had a working flight search and no way to
name the airport, so it asked again, and again.

This adds the other 7,800. `airportsdata` ships them as a package: IATA code, city,
country, name, across 233 countries. Offline, versioned, no network on the hot
path, and — the property that matters — **it cannot invent an airport**. A model
asked for "the code for Hanoi" will confidently produce one for a city it has
never heard of; a lookup either finds a row or does not.

Resolution is layered, most specific first:

1. **The curated table.** It encodes intent rather than geography: "Bali" is an
   island whose airport is at Denpasar, and "Goa" should mean the airport people
   actually fly into. Data cannot know that; a person had to decide it.
2. **City name in the dataset.** Handles the long tail — Hanoi, Da Nang, Tbilisi,
   Porto — with no work from us.
3. **Airport name.** So "Heathrow" and "Changi" resolve even though neither is a
   city.

A country name deliberately resolves to nothing. "Vietnam" is not an airport, and
picking Hanoi over Ho Chi Minh City on the user's behalf is exactly the kind of
confident guess that produces a real price for the wrong place.
"""

from __future__ import annotations

import functools
import re
import unicodedata

# Cities with several airports where the choice is not ours to make arbitrarily.
# Only needed where the dataset is ambiguous and the answer is well known; the
# curated table in places.py already covers the routes this product cares about.
PRIMARY_AIRPORT: dict[tuple[str, str], str] = {
    ("london", "GB"): "LHR",
    ("new york", "US"): "JFK",
    ("paris", "FR"): "CDG",
    ("tokyo", "JP"): "HND",
    ("milan", "IT"): "MXP",
    ("rome", "IT"): "FCO",
    ("moscow", "RU"): "SVO",
    ("beijing", "CN"): "PEK",
    ("shanghai", "CN"): "PVG",
    ("sao paulo", "BR"): "GRU",
    ("buenos aires", "AR"): "EZE",
    ("toronto", "CA"): "YYZ",
    ("chicago", "US"): "ORD",
    ("washington", "US"): "IAD",
    ("houston", "US"): "IAH",
    ("osaka", "JP"): "KIX",
    ("jakarta", "ID"): "CGK",
    ("seoul", "KR"): "ICN",
    ("berlin", "DE"): "BER",
    ("stockholm", "SE"): "ARN",
    ("oslo", "NO"): "OSL",
    ("istanbul", "TR"): "IST",
    ("tehran", "IR"): "IKA",
    ("dubai", "AE"): "DXB",
}

# Ordinary English words that are also the names of real airports. Ignored when
# they appear inside a longer phrase; an exact match on one still resolves, since
# "Nice" on its own can only mean the city.
AMBIGUOUS_WORDS = frozenset({
    "nice", "split", "mobile", "reading", "bath", "hope", "industry", "normal",
    "why", "boring", "eagle", "sun", "beach", "home", "work", "best", "warm",
    "hot", "cold", "cheap", "long", "short", "north", "south", "east", "west",
    "central", "city", "town", "island", "lake", "river", "bay", "port", "new",
    "old", "big", "little", "grand", "saint", "st", "san", "santa", "may",
    "march", "august", "sunday", "monday", "friday", "one", "two", "three",
})

_PUNCT = re.compile(r"[^\w\s]+")
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return _WS.sub(" ", _PUNCT.sub(" ", folded.lower())).strip()


@functools.lru_cache(maxsize=1)
def _dataset() -> dict:
    """The raw IATA-keyed table. Cached: built once per process."""
    import airportsdata

    return airportsdata.load("IATA")


@functools.lru_cache(maxsize=1)
def _index() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """(city -> code, airport name -> code, code -> country).

    Ambiguity is resolved once, here, so a lookup is a dict hit and two calls for
    the same city can never disagree.
    """
    airports = _dataset()
    countries = {code: a["country"] for code, a in airports.items()}

    by_city: dict[str, list[tuple[str, dict]]] = {}
    for code, a in airports.items():
        city = normalize(a["city"])
        if city:
            by_city.setdefault(city, []).append((code, a))

    cities: dict[str, str] = {}
    for city, options in by_city.items():
        cities[city] = _pick(city, options)

    names: dict[str, str] = {}
    for code, a in airports.items():
        name = normalize(a["name"])
        city = normalize(a["city"])
        stripped = name
        for noise in (" international", " airport", " airfield", " airbase"):
            stripped = stripped.replace(noise, "")
        stripped = stripped.strip()
        # And the distinctive part: "london heathrow" -> "heathrow". Without this
        # neither "Heathrow" nor "Changi" resolved to anything at all.
        distinctive = stripped
        if city and stripped.startswith(city + " "):
            distinctive = stripped[len(city) + 1 :].strip()

        for token in (name, stripped, distinctive):
            token = token.strip()
            # Never let an airport name shadow a city name — "Bengaluru
            # International Airport" must not outrank the city "Bengaluru".
            if token and token not in cities:
                names.setdefault(token, code)

    return cities, names, countries


def _pick(city: str, options: list[tuple[str, dict]]) -> str:
    """Choose one airport for a city, deterministically.

    Order matters: a hand-picked primary beats a name heuristic, and the heuristic
    beats alphabetical — but something has to be last, or the same city resolves
    differently between runs depending on dict ordering.
    """
    if len(options) == 1:
        return options[0][0]

    # Check every country this city name appears in, not just whichever row the
    # dataset happened to yield first. "London" exists in GB and in Ontario, and
    # consulting only the first one resolved it to Biggin Hill.
    for _, airport in options:
        curated = PRIMARY_AIRPORT.get((city, airport["country"]))
        if curated and any(code == curated for code, _ in options):
            return curated

    international = [c for c, a in options if "International" in a["name"]]
    if len(international) == 1:
        return international[0]

    pool = international or [c for c, _ in options]
    return sorted(pool)[0]


def lookup(name: str) -> str | None:
    """A place as a person said it -> an IATA code, or None.

    None is a real answer. It means "no airport is named that", and the caller
    should ask rather than guess.
    """
    key = normalize(name)
    if not key:
        return None

    cities, names, countries = _index()

    # An explicit code, only if it exists.
    if len(key) == 3 and key.upper() in countries:
        return key.upper()

    if key in cities:
        return cities[key]
    if key in names:
        return names[key]

    # A place named inside a phrase: "flights to da nang please".
    #
    # Single-word candidates are filtered here, and only here. There really are
    # airports at Nice, Split, Mobile, Reading and Bath, so "somewhere nice"
    # resolved to Nice, France and "a split decision" to an airport in North
    # Dakota. Asked for exactly "Nice" the answer is obviously the city — the
    # ambiguity only exists when the word turns up inside a longer sentence,
    # which is precisely where a wrong match becomes a real price for a place
    # nobody mentioned.
    words = key.split()
    for size in range(min(4, len(words)), 0, -1):
        for i in range(len(words) - size + 1):
            candidate = " ".join(words[i : i + size])
            if size == 1 and (
                candidate in AMBIGUOUS_WORDS or len(candidate) < 3
            ):
                # Sub-three-letter words inside a phrase are never a place
                # someone meant: "a split decision" matched an airport off the
                # bare word "a".
                continue
            if candidate in cities:
                return cities[candidate]
            if candidate in names:
                return names[candidate]
    return None


def country_of(code: str | None) -> str | None:
    if not code:
        return None
    return _index()[2].get(code.upper())


def describe(code: str | None) -> str | None:
    """City and country for a code, for logs and for confirming out loud."""
    if not code:
        return None
    a = _dataset().get(code.upper())
    return f"{a['city']}, {a['country']}" if a else None


def exists(code: str | None) -> bool:
    """Whether a code is a real airport. The guard on anything a model suggests."""
    return bool(code) and code.upper() in _dataset()


def city_name(code: str | None) -> str | None:
    """Just the city, for saying out loud. "Denpasar-Bali Island" is a dataset
    label rather than a word anyone uses, so the first segment is enough."""
    if not code:
        return None
    airport = _dataset().get(code.upper())
    if not airport:
        return None
    return (airport["city"].split("-")[0].split("/")[0]).strip() or None


def airport_name(code: str | None) -> str | None:
    """The airport's own name. Sometimes the only place the recognisable name
    appears: ZNZ is filed under the city "Kiembi Samaki" but named "Abeid Amani
    Karume International" for Zanzibar."""
    if not code:
        return None
    airport = _dataset().get(code.upper())
    return airport["name"] if airport else None
