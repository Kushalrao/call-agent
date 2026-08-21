"""One true thing about a city, said while the flight search runs.

No API calls — the parsing and the rules are what these test. The eight seconds
between "checking Bangkok flights" and a price is the longest silence in the whole
interaction, and this is what goes in it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.citynote import MAX_TOKENS, SYSTEM, TIMEOUT_S, CityNotes, parse_note


def resp(text: str):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


def test_parses_a_note():
    note = parse_note(resp('{"note":"Everyone eats on plastic stools by the road."}'))
    assert note == "Everyone eats on plastic stools by the road."


def test_a_paragraph_is_discarded_rather_than_trusted():
    """It is spoken over a wait. A model that ignored "one sentence" would still
    be talking when the prices arrive."""
    long = " ".join(["word"] * 40)
    assert parse_note(resp('{"note":"%s"}' % long)) == ""


def test_an_empty_note_is_a_valid_answer():
    """Silence beats a confident invention about somewhere the person is about to
    fly — they cannot check it, and it makes everything else less believable."""
    assert parse_note(resp('{"note":""}')) == ""


@pytest.mark.parametrize("text", ["not json", "{}", "[]", ""])
def test_a_bad_response_is_not_spoken(text):
    assert parse_note(resp(text)) == ""


def test_thinking_blocks_are_skipped():
    response = SimpleNamespace(content=[
        SimpleNamespace(type="thinking", text=""),
        SimpleNamespace(type="text", text='{"note":"A real detail."}'),
    ])
    assert parse_note(response) == "A real detail."


# --- caching ---------------------------------------------------------------


def test_cached_returns_nothing_before_generation():
    assert CityNotes().cached("HAN") is None


def test_an_empty_code_never_calls_a_model():
    import asyncio
    assert asyncio.run(CityNotes().note_for("")) == ""


# --- the contract ---------------------------------------------------------


def test_the_timeout_is_shorter_than_a_search():
    """It has to be spoken before the prices land, or it has missed the silence it
    exists to fill."""
    assert TIMEOUT_S <= 4.0


def test_the_prompt_forbids_inventing_and_forbids_borrowing():
    flat = " ".join(SYSTEM.split())
    # An earlier version listed real example sentences for Delhi, Hanoi and
    # Colombo, and the model returned them verbatim for exactly those cities.
    assert "Do not reuse this sentence" in flat
    assert "return an empty string" in flat
    assert "Never mention flights, prices, airlines" in flat


def test_output_is_bounded():
    assert MAX_TOKENS <= 300
