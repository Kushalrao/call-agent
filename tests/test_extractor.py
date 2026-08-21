"""Trip extraction (agent/extractor.py). No API calls — every test is free."""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from agent.extractor import (
    MIN_INTERVAL_S,
    SCHEMA,
    SYSTEM,
    TripExtractor,
    parse_extraction,
    worth_extracting,
)


# --- the local gate that keeps cost down ----------------------------------


@pytest.mark.parametrize("text", [
    "Bali sounds good",
    "second week of December",
    "under 30 thousand",
    "direct flights only",
    "we fly out of Bangalore",
    "maybe the 10th",
])
def test_speech_that_could_carry_trip_detail_is_extracted(text):
    assert worth_extracting(text)


@pytest.mark.parametrize("text", [
    "yeah", "hmm ok", "one sec", "", "   ", "I said what I said",
    "no that's fine", "hello can you hear me",
])
def test_ordinary_speech_costs_no_model_call(text):
    """Most utterances in a call carry nothing extractable. Sending them all to a
    model doubles the per-call spend for no information."""
    assert not worth_extracting(text)


def test_the_gate_is_generous_rather_than_clever():
    """"That was a great trip" passes the gate even though it is reminiscing.
    That is correct: the gate only decides whether to look, and intent_strength
    decides what it means. A cheap false positive beats a missed plan."""
    assert worth_extracting("That was a great trip")


# --- cadence --------------------------------------------------------------


def test_extraction_has_a_floor_on_frequency():
    e = TripExtractor(api_key="k")
    assert e.due(now=1000.0)
    e._last_call_at = 1000.0
    assert not e.due(now=1000.0 + MIN_INTERVAL_S - 0.1)
    assert e.due(now=1000.0 + MIN_INTERVAL_S + 0.1)


# --- parsing is defensive ------------------------------------------------


def resp(text: str):
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


def test_parses_a_well_formed_extraction():
    out = parse_extraction(resp(
        '{"destination":"Bali","depart_date":"2026-12-10","intent_strength":0.8}'
    ))
    assert out == {"destination": "Bali", "depart_date": "2026-12-10",
                   "intent_strength": 0.8}


def test_unknown_fields_are_dropped_rather_than_merged():
    """The trip context must never be corrupted by a field we did not ask for."""
    out = parse_extraction(resp('{"destination":"Bali","sneaky":"x","intent_strength":0.5}'))
    assert out is not None and "sneaky" not in out


@pytest.mark.parametrize("text", ["not json", "[]", "null", '"a string"', ""])
def test_a_bad_response_leaves_the_context_untouched(text):
    assert parse_extraction(resp(text)) is None


def test_no_content_does_not_raise():
    assert parse_extraction(SimpleNamespace(content=[])) is None


# --- the contract ---------------------------------------------------------


def test_every_field_is_optional_except_intent():
    assert SCHEMA["required"] == ["intent_strength"]
    assert SCHEMA["additionalProperties"] is False


def test_the_prompt_says_a_place_is_not_an_intention():
    flat = " ".join(SYSTEM.split())
    assert "Mentioning a city is not intent" in flat
    assert "reminiscing" in flat


def test_the_prompt_forbids_inventing_airport_codes():
    assert "Never invent an airport code" in " ".join(SYSTEM.split())


def test_the_prompt_states_the_injection_stance():
    assert "untrusted input" in " ".join(SYSTEM.split())
