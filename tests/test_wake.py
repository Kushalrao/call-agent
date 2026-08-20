"""Fast-path wake detection (spec section 5.1a). Pure logic, no audio."""

from __future__ import annotations

import pytest

from agent.wake import FastPath, WakeWordMatcher, normalize


@pytest.fixture
def m() -> WakeWordMatcher:
    return WakeWordMatcher()


# --- the ways STT actually mangles the name ---------------------------------


@pytest.mark.parametrize("text", [
    "hey copilot find us flights",
    "hey co pilot",              # split into two words
    "Co-Pilot, help us out",     # hyphen + capitals + comma
    "copilot?",
    "hey copilots",              # plural
    "COPILOT",
    "ok copilot what about december",
    "hey  copilot",              # doubled whitespace
])
def test_name_is_found_however_it_arrives(m, text):
    assert m.match(text).matched, text


@pytest.mark.parametrize("text", [
    "the cockpit door was open",
    "we should fly to Bali",
    "call the pilot",
    "",
    "december is packed at work man",
    "co",                        # a prefix is not the name
])
def test_near_misses_do_not_fire(m, text):
    assert not m.match(text).matched, text


def test_a_wrong_fire_is_worse_than_a_missed_one():
    """Firing on "cockpit" or "pilot" means the agent interjects into a
    conversation that was not addressing it. That is the expensive error."""
    for text in ["cockpit", "pilot", "copy that", "coppola", "capital"]:
        assert not WakeWordMatcher().match(text).matched, text


def test_custom_wake_name():
    m = WakeWordMatcher("jarvis")
    assert m.match("hey jarvis").matched
    assert not m.match("hey copilot").matched


def test_empty_wake_name_is_rejected_at_construction():
    with pytest.raises(ValueError):
        WakeWordMatcher("...")


def test_normalize():
    assert normalize("  Hey, Co-Pilot!!  ") == "hey co pilot"


# --- travel keywords are a separate, weaker signal -------------------------


def test_travel_keywords_never_fire_on_their_own(m):
    """Section 5.1a may only ADD triggers. Deciding intent from travel words is
    5.1b's job, at its own confidence bar — the fast path only warms a prefetch."""
    result = m.match("we should book flights to Bali")
    assert result.matched is False
    assert "flights" in result.travel_keywords
    assert "book" in result.travel_keywords


def test_bare_wake_name_fires_without_any_travel_word(m):
    """The spec's "name plus keyword gate" is two signals, not a conjunction:
    requiring a travel word would suppress a bare "hey copilot?"."""
    result = m.match("hey copilot")
    assert result.matched is True
    assert result.travel_keywords == ()


# --- interim stream bookkeeping --------------------------------------------


def test_one_spoken_phrase_fires_exactly_once():
    """Interims are cumulative. A naive matcher fires once per interim, so the
    agent would trigger four times for one "hey copilot find us flights"."""
    fp = FastPath()
    interims = ["hey", "hey co", "hey copilot", "hey copilot find", "hey copilot find us"]
    fires = [fp.on_interim("u-rohan", t) for t in interims]
    assert sum(1 for f in fires if f) == 1
    assert fires[2] is not None, "should fire as soon as the name completes"


def test_the_next_utterance_can_fire_again():
    fp = FastPath()
    assert fp.on_interim("u-rohan", "hey copilot")
    fp.on_final("u-rohan")
    assert fp.on_interim("u-rohan", "copilot again")


def test_two_speakers_are_tracked_independently():
    """Both people can be mid-utterance at once; they are separate segments."""
    fp = FastPath()
    assert fp.on_interim("u-rohan", "hey copilot")
    assert fp.on_interim("u-kushal", "copilot yes")   # not suppressed by Rohan
    assert fp.on_interim("u-rohan", "hey copilot please") is None


def test_final_for_one_speaker_does_not_reset_the_other():
    fp = FastPath()
    fp.on_interim("u-rohan", "hey copilot")
    fp.on_interim("u-kushal", "hey copilot")
    fp.on_final("u-rohan")
    assert fp.on_interim("u-rohan", "copilot") is not None
    assert fp.on_interim("u-kushal", "copilot") is None


def test_warming_is_reported_once_per_segment():
    fp = FastPath()
    assert fp.should_warm("u-rohan", "we should book flights") != ()
    assert fp.should_warm("u-rohan", "we should book flights to bali") == ()
    fp.on_final("u-rohan")
    assert fp.should_warm("u-rohan", "flights again") != ()


def test_warming_needs_no_wake_word():
    fp = FastPath()
    assert "flights" in fp.should_warm("u-rohan", "what about flights though")


def test_the_wake_name_is_in_the_stt_keyterms():
    """A live call transcribed "Hey copilot" as "Echo Pilot", so the fast path
    never matched and a direct question went unanswered. Every other keyterm only
    affects what the agent does; this one decides whether it reacts at all."""
    from agent.vocabulary import KEYTERMS
    lowered = {k.lower() for k in KEYTERMS}
    assert "copilot" in lowered
    assert "co-pilot" in lowered
