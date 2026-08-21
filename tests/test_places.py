"""City name -> IATA (agent/places.py).

The failure this guards against is the quiet one: a wrong code does not error, it
searches the wrong city, and the agent then reads a genuine price for the wrong
place aloud. So the table is authoritative and anything unknown resolves to None.
"""

from __future__ import annotations

import pytest

from agent.places import (
    CITY_TO_IATA,
    KNOWN_CODES,
    is_indian,
    missing_codes,
    normalize_place,
    resolve,
    spoken_name,
)
from agent.vocabulary import DESTINATIONS, ORIGINS


# --- the invariant that keeps hearing and resolving in sync -----------------


def test_every_vocabulary_place_has_a_code():
    """`vocabulary.py` is what Deepgram is biased toward, so anything the agent
    can reliably hear must be resolvable. If the two lists drift, the symptom is
    "the agent ignored me" rather than "that city has no code"."""
    assert missing_codes() == [], (
        f"these keyterms have no IATA code: {missing_codes()}"
    )


def test_the_two_lists_are_checked_in_both_directions():
    """A code with no spoken form is dead weight, but harmless. The other
    direction is the dangerous one, covered above."""
    assert len(CITY_TO_IATA) >= len(set(ORIGINS) | set(DESTINATIONS))


# --- resolution ------------------------------------------------------------


@pytest.mark.parametrize("spoken,code", [
    ("Bali", "DPS"), ("bali", "DPS"), ("Denpasar", "DPS"),
    ("Bangalore", "BLR"), ("Bengaluru", "BLR"),
    ("Bombay", "BOM"), ("Mumbai", "BOM"),
    ("Ho Chi Minh City", "SGN"), ("Saigon", "SGN"),
    ("new york", "JFK"),
    ("Maldives", "MLE"), ("Male", "MLE"),
])
def test_aliases_land_on_the_same_code(spoken, code):
    assert resolve(spoken) == code


@pytest.mark.parametrize("phrase,code", [
    ("flying to Bali", "DPS"),
    ("out of Bangalore", "BLR"),
    ("Bali, Indonesia", "DPS"),
])
def test_a_city_inside_a_phrase_is_found(phrase, code):
    assert resolve(phrase) == code


def test_the_longest_match_wins():
    """"new york" must not be shadowed by a shorter accidental match."""
    assert resolve("flights to new york please") == "JFK"


def test_punctuation_and_accents_do_not_matter():
    assert resolve("Bengalūru,") == "BLR"
    assert normalize_place("  Ho-Chi-Minh!! ") == "ho chi minh"


# --- refusing to guess -----------------------------------------------------


@pytest.mark.parametrize("unknown", [
    "Atlantis", "", None, "   ", "somewhere nice", "XYZ", "zzz",
])
def test_an_unknown_place_resolves_to_none_not_a_guess(unknown):
    """Silence is recoverable. A confident price for the wrong city is not."""
    assert resolve(unknown) is None


def test_an_unrecognised_three_letter_code_is_rejected():
    """The model volunteering a plausible-looking code is exactly the failure
    mode this table exists to prevent."""
    assert "QQQ" not in KNOWN_CODES
    assert resolve("QQQ") is None


def test_a_known_code_passes_through_in_any_casing():
    assert resolve("DPS") == "DPS"
    assert resolve("dps") == "DPS"


# --- helpers ---------------------------------------------------------------


def test_domestic_detection_matches_the_extension_split():
    assert is_indian("BLR") and is_indian("DEL")
    assert not is_indian("DPS") and not is_indian("DXB")
    assert not is_indian(None)


def test_spoken_name_round_trips():
    assert spoken_name(resolve("Bali")) == "Bali"
    assert spoken_name(None) == ""


def test_spoken_name_falls_back_to_the_code():
    """Ugly, never wrong."""
    assert spoken_name("ZZZ") == "ZZZ"


# --- failure wording -------------------------------------------------------


def test_a_broken_search_does_not_sound_like_an_empty_one():
    """A live run said "I couldn't find flights to Bali" when the real problem
    was a wrong extension path. A fact about the world and a bug on our side must
    not produce the same sentence."""
    from agent.search import SearchOutcome, to_sentence

    broke = to_sentence(SearchOutcome("BLR", "DPS", "2026-08-31", error="boom"))
    empty = to_sentence(SearchOutcome("BLR", "DPS", "2026-08-31"))
    assert broke != empty
    assert "couldn't search" in broke
    assert "didn't find" in empty


# --- direction ------------------------------------------------------------


def test_from_x_to_y_does_not_search_x_to_x():
    """A live run produced `from=BLR&to=BLR` — a search from Bangalore to
    Bangalore — because resolve() scans left to right and "Bangalore" comes
    before "Bali" in "flights from Bangalore to Bali"."""
    from agent.search import destination_from, origin_from, split_route

    text = "hey copilot flights from Bangalore to Bali"
    assert split_route(text) == ("BLR", "DPS")
    assert destination_from(text) == "DPS"
    assert origin_from(text) == "BLR"


import pytest as _pytest


@_pytest.mark.parametrize("text,origin,destination", [
    ("hey copilot find me flights to Singapore", None, "SIN"),
    ("hey copilot flights from Bangalore to Bali", "BLR", "DPS"),
    # Reversed marker order.
    ("copilot, flights to Dubai from Delhi", "DEL", "DXB"),
    ("hey copilot, we fly out of Bangalore to Kathmandu", "BLR", "KTM"),
    # No direction stated: one place can only be the destination.
    ("copilot Bali flights", None, "DPS"),
    ("hey copilot flights to Atlantis", None, None),
])
def test_route_direction(text, origin, destination):
    from agent.search import split_route
    assert split_route(text) == (origin, destination)


def test_origin_falls_back_to_the_default_when_unstated():
    """People almost never say where they are flying from."""
    from agent.search import DEFAULT_ORIGIN, origin_from
    assert origin_from("hey copilot flights to Singapore") == DEFAULT_ORIGIN


def test_a_route_to_where_you_already_are_is_refused_not_searched():
    """"flights to Bangalore" resolves BLR, and the origin default is also BLR,
    which would build a from=BLR&to=BLR URL. There is no sensible search for
    that, so it is refused at the choke point and answered instead."""
    import asyncio

    from agent.search import run_search, to_sentence

    outcome = asyncio.run(run_search("BLR", call_id="t", origin="BLR"))
    assert not outcome.ok
    assert outcome.error == "same_city"
    assert outcome.elapsed_s == 0.0, "refused before any browser work"
    assert "already in" in to_sentence(outcome)


# --- latency masking -------------------------------------------------------


def test_acknowledgement_names_the_destination():
    """Spoken within ~1s of being addressed, to cover the ~10s search. Silence
    after a direct question reads as "it didn't hear me", and people repeat
    themselves — which then reads as a second request."""
    from agent.search import to_acknowledgement

    assert "Singapore" in to_acknowledgement("SIN")
    assert "Dubai" in to_acknowledgement("DXB")


def test_the_two_utterances_stay_within_the_free_tier_budget():
    """Free ElevenLabs is ~10k characters. Two remarks per request means the
    preamble is charged on every single one, so it has to stay short."""
    from agent.search import Cheapest, SearchOutcome, to_acknowledgement, to_sentence

    outcome = SearchOutcome("BLR", "SIN", "2026-08-31",
                            Cheapest("Cleartrip", "Air India", "AI2", 19532, 1))
    per_request = len(to_acknowledgement("SIN")) + len(to_sentence(outcome))
    assert per_request < 140, "two utterances per request must stay compact"
    assert 10_000 // per_request >= 70, "free tier should cover ~100 requests"


def test_an_unknown_code_still_produces_a_sayable_acknowledgement():
    from agent.search import to_acknowledgement
    assert to_acknowledgement("ZZZ").strip() != ""
