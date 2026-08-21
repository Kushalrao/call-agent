"""Resolving any place in the world (agent/airports.py + agent/places.py).

The curated table covered about forty cities, which was fine while the only
consumer was a call about a handful of routes. The moment someone said "Hanoi" the
agent had a working flight search and no way to name the airport, so it asked
which city — and kept asking.

These tests are mostly about the two ways this goes wrong: refusing a place that
is real, and accepting a place nobody meant.
"""

from __future__ import annotations

import pytest

from agent.airports import AMBIGUOUS_WORDS, city_name, country_of, exists, lookup
from agent.places import is_indian, resolve


# --- the long tail now resolves -------------------------------------------


@pytest.mark.parametrize("place,code", [
    ("Hanoi", "HAN"),
    ("Da Nang", "DAD"),
    ("Ho Chi Minh City", "SGN"),
    ("Tbilisi", "TBS"),
    ("Porto", "OPO"),
    ("Reykjavik", "KEF"),
    ("Cairo", "CAI"),
    ("Coimbatore", "CJB"),
    ("Cusco", "CUZ"),
])
def test_cities_the_curated_table_never_had(place, code):
    assert resolve(place) == code


def test_airport_names_resolve_too():
    """"Heathrow" and "Changi" are not cities, and people say them anyway."""
    assert resolve("Heathrow") == "LHR"
    assert resolve("Changi") == "SIN"


# --- the curated table still wins -----------------------------------------


def test_curated_intent_beats_global_geography():
    """The dataset contains a village called Bali in Cameroon with its own
    airstrip. "Bali" from someone planning a holiday means the island, whose
    airport is at Denpasar. Data cannot know that; a person decided it."""
    assert resolve("Bali") == "DPS"
    assert lookup("Bali") == "BLC", "the raw dataset really does say Cameroon"


def test_ambiguous_cities_land_on_the_expected_airport():
    """London has seven airports and the dataset yields them in no useful order.
    Before the primary list, "London" resolved to Biggin Hill."""
    assert resolve("London") == "LHR"
    assert resolve("New York") == "JFK"
    assert resolve("Paris") == "CDG"


def test_the_same_city_always_resolves_the_same_way():
    """Ambiguity is settled once, when the index is built. Two calls that
    disagreed would be worse than either answer."""
    assert {resolve("London") for _ in range(5)} == {"LHR"}


# --- refusing to guess -----------------------------------------------------


@pytest.mark.parametrize("place", ["Vietnam", "Sri Lanka", "Japan", "Atlantis", ""])
def test_a_country_is_not_an_airport(place):
    """Picking Hanoi over Ho Chi Minh City on someone's behalf is exactly the
    confident guess that produces a real price for the wrong place."""
    assert resolve(place) is None


@pytest.mark.parametrize("phrase", ["somewhere nice", "a split decision", "somewhere warm"])
def test_common_words_inside_a_phrase_are_not_places(phrase):
    """There really are airports at Nice, Split, Mobile, Reading and Bath."""
    assert resolve(phrase) is None


def test_but_the_exact_word_still_resolves():
    """Asked for exactly "Nice", the answer is obviously the city. The ambiguity
    only exists inside a longer sentence."""
    assert resolve("Nice") == "NCE"
    assert resolve("Split") == "SPU"


def test_the_ambiguous_list_is_lowercase_and_bounded():
    assert all(w == w.lower() for w in AMBIGUOUS_WORDS)
    assert len(AMBIGUOUS_WORDS) < 100


# --- domestic detection ---------------------------------------------------


def test_domestic_detection_covers_every_indian_airport():
    """It used to be a hand-written list of eighteen, so a domestic route the
    code thought was international was searched two weeks out instead of four
    days."""
    for code in ("CJB", "IXM", "BBI", "GAU", "TRZ", "VNS"):
        assert is_indian(code), code
    for code in ("DPS", "DXB", "HAN", "LHR"):
        assert not is_indian(code), code


# --- helpers -------------------------------------------------------------


def test_exists_is_the_guard_on_anything_suggested():
    assert exists("HAN")
    assert not exists("QQQ")
    assert not exists(None)


def test_city_name_is_speakable():
    """"Denpasar-Bali Island" is a dataset label, not a word anyone says."""
    assert city_name("DPS") == "Denpasar"
    assert city_name("HAN") == "Hanoi"
    assert city_name("QQQ") is None


def test_country_of():
    assert country_of("HAN") == "VN"
    assert country_of("BLR") == "IN"
    assert country_of(None) is None


# --- route parsing --------------------------------------------------------


@pytest.mark.parametrize("phrase,origin,destination", [
    ("flights to Nice from Bangalore", "BLR", "NCE"),
    ("flights to Dubai from Delhi", "DEL", "DXB"),
    ("flights from Bangalore to Bali", "BLR", "DPS"),
    ("I want to fly to Hanoi", None, "HAN"),
    ("we fly out of Bangalore to Kathmandu", "BLR", "KTM"),
])
def test_route_direction_survives_the_wider_dataset(phrase, origin, destination):
    """"flights to Nice from Bangalore" resolved to Bangalore as the destination:
    the tail held both cities and was resolved whole."""
    from agent.search import split_route

    assert split_route(phrase) == (origin, destination)
