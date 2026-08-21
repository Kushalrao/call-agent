"""Accumulated trip context (agent/trip.py). Pure data, no network.

This is the ambient path's memory, and the reason nobody has to say the agent's
name. It is also where a single mishearing could quietly redirect a real flight
search, so most of these tests are about refusing to be convinced too easily.
"""

from __future__ import annotations

from agent.trip import (
    CORROBORATION_NEEDED,
    HIGH_CONFIDENCE,
    SETTLE_SECONDS,
    Field,
    TripContext,
    TripTracker,
)

T = 1000.0


# --- corroboration ---------------------------------------------------------


def test_one_mention_is_not_enough():
    """A live call transcribed "Fine. Flies to Srinagar." and Srinagar is a real
    airport in the table. One utterance must never pick a destination."""
    f = Field().add("SXR", 0.4, T)
    assert f.value == "SXR"
    assert not f.confirmed
    assert not f.usable


def test_two_mentions_confirm():
    f = Field().add("DPS", 0.5, T).add("DPS", 0.5, T + 2)
    assert f.confirmed and f.usable
    assert f.mentions == CORROBORATION_NEEDED


def test_one_confident_mention_confirms():
    """"We're going to Bali on the tenth" is not a mishearing."""
    f = Field().add("DPS", HIGH_CONFIDENCE, T)
    assert f.confirmed and f.usable


def test_interleaved_noise_cannot_erase_real_evidence():
    """The bug the first design had: "Bali", then a mishearing, then "Bali" again.
    A single slot overwrote each time, so Bali's two mentions never accumulated
    and nothing was ever confirmed. Support is tracked per candidate instead."""
    f = (
        Field()
        .add("DPS", 0.5, T)        # "Bali sounds good"
        .add("SXR", 0.4, T + 2)    # "Fine. Flies to Srinagar."
        .add("DPS", 0.6, T + 4)    # "so Bali in December then"
    )
    assert f.value == "DPS", "two mentions must beat one"
    assert f.confirmed


def test_mentions_outrank_confidence():
    """Two independent mentions are stronger evidence than one transcript that
    happened to sound confident."""
    f = Field().add("DPS", 0.5, T).add("DPS", 0.5, T + 1).add("SXR", 0.85, T + 2)
    assert f.value == "DPS"


def test_a_genuine_change_of_mind_wins_when_asserted_clearly():
    f = Field().add("DPS", 0.5, T).add("DPS", 0.5, T + 1)
    changed = f.add("BKK", 0.95, T + 5).add("BKK", 0.95, T + 6)
    assert changed.value == "BKK" and changed.confirmed


def test_contested_is_visible():
    f = Field().add("DPS", 0.5, T).add("SXR", 0.5, T + 1)
    assert f.contested


def test_the_candidate_tally_is_bounded():
    f = Field()
    for i in range(20):
        f = f.add(f"X{i}", 0.5, T + i)
    assert len(f.candidates) <= 4


# --- the conversation ------------------------------------------------------


def conversation() -> TripTracker:
    t = TripTracker(default_origin="BLR")
    t.merge({}, confidence=0.5, now=T)
    t.merge({"destination": "Bali", "intent_strength": 0.4}, confidence=0.5, now=T + 2)
    t.merge({"destination": "Srinagar"}, confidence=0.4, now=T + 4)
    t.merge({"destination": "Bali", "depart_date": "2026-12-10",
             "intent_strength": 0.8}, confidence=0.6, now=T + 6)
    return t


def test_a_real_conversation_converges():
    t = conversation()
    assert t.context.route == (None, "DPS")
    assert t.context.is_searchable(default_origin="BLR")
    assert t.parts() == ("BLR", "DPS")


def test_places_are_resolved_through_the_curated_table():
    """A city the model invented must never reach a search URL."""
    t = TripTracker(default_origin="BLR")
    t.merge({"destination": "Atlantis"}, confidence=0.95, now=T)
    assert t.context.destination.value is None


def test_intent_strength_is_kept_separate_from_the_route():
    """Mentioning a city is not an intention. A complete route is not permission
    to act on it."""
    t = TripTracker(default_origin="BLR")
    t.merge({"destination": "Goa", "intent_strength": 0.1}, confidence=0.95, now=T)
    assert t.context.is_searchable(default_origin="BLR")
    assert t.route_to_answer(now=T + 100) is None, "reminiscing must not answer"


def test_intent_strength_only_rises():
    t = TripTracker()
    t.merge({"intent_strength": 0.8}, now=T)
    t.merge({"intent_strength": 0.1}, now=T + 1)
    assert t.context.intent_strength == 0.8


# --- settling --------------------------------------------------------------


def test_answering_waits_for_the_route_to_stop_moving():
    """"Maybe Bali... actually Thailand" must not produce two spoken answers."""
    t = conversation()
    assert t.route_to_answer(now=T + 6 + SETTLE_SECONDS - 0.1) is None
    assert t.route_to_answer(now=T + 6 + SETTLE_SECONDS + 0.1) is not None


def test_prefetch_does_not_wait_for_settling():
    """A discarded search costs nothing; having it warm is what makes the answer
    instant."""
    t = conversation()
    assert t.route_to_prefetch() is not None


def test_each_route_is_prefetched_once():
    """The aggregators throttle repeated queries — one route was blocked during
    testing by exactly this."""
    t = conversation()
    assert t.route_to_prefetch() is not None
    assert t.route_to_prefetch() is None


def test_each_route_is_answered_once():
    t = conversation()
    assert t.route_to_answer(now=T + 20) is not None
    assert t.route_to_answer(now=T + 21) is None


def test_a_new_route_is_answerable_again():
    t = conversation()
    assert t.route_to_answer(now=T + 20) is not None
    t.merge({"destination": "Dubai", "intent_strength": 0.9}, confidence=0.95, now=T + 22)
    assert t.route_to_answer(now=T + 22 + SETTLE_SECONDS + 1) is not None


def test_a_route_to_where_you_are_is_not_searchable():
    t = TripTracker(default_origin="BLR")
    t.merge({"destination": "Bangalore"}, confidence=0.95, now=T)
    assert not t.context.is_searchable(default_origin="BLR")
    assert t.route_to_prefetch() is None


# --- rendering -------------------------------------------------------------


def test_render_shows_only_confirmed_values():
    t = TripTracker(default_origin="BLR")
    t.merge({"destination": "Bali"}, confidence=0.4, now=T)
    assert "Bali" not in t.context.render()
    t.merge({"destination": "Bali"}, confidence=0.4, now=T + 1)
    assert "Bali" in t.context.render()


def test_empty_context_renders_without_error():
    assert TripContext().render() == "(nothing yet)"


def test_summary_flags_pending_fields():
    t = TripTracker()
    t.merge({"budget_inr": 30000}, confidence=0.4, now=T)
    assert t.context.summary()["budget_inr_pending"] is True


def test_a_clear_decision_beats_two_passing_mentions():
    """Caught by a test rather than a call: with mentions alone, someone saying
    "actually, let us do Dubai" once and clearly lost permanently to a place
    mentioned twice in passing. That is not caution, it is being unable to hear a
    decision. A high-confidence assertion counts double, and ties break toward
    the most recent."""
    f = (
        Field()
        .add("DPS", 0.6, T)
        .add("DPS", 0.6, T + 1)
        .add("DXB", 0.95, T + 10)
    )
    assert f.value == "DXB" and f.confirmed


def test_but_a_low_confidence_change_still_loses():
    f = Field().add("DPS", 0.6, T).add("DPS", 0.6, T + 1).add("DXB", 0.5, T + 10)
    assert f.value == "DPS"


# --- evidence for speaking -------------------------------------------------


def _tracker(steps, *, origin="BLR"):
    t = TripTracker(default_origin=origin)
    for extracted, confidence, when in steps:
        t.merge(extracted, confidence=confidence, now=when)
    return t


def test_reminiscing_never_earns_speech():
    """A detailed story about last year's holiday in Goa mentions Goa
    confidently. It is not a trip being planned."""
    t = _tracker([({"destination": "Goa", "intent_strength": 0.1}, 0.95, T)])
    assert t.evidence(now=T + 30) < 0.5


def test_active_planning_earns_speech():
    """The case a live rehearsal got wrong: it searched, found the fare, and
    stayed silent, because the gate used one utterance's confidence."""
    from agent.policy import PROACTIVE_SPEECH_MIN_CONFIDENCE

    t = _tracker([
        ({"destination": "Singapore", "intent_strength": 0.7}, 0.7, T),
        ({"destination": "Singapore"}, 0.7, T + 2),
    ])
    assert t.evidence(now=T + 30) >= PROACTIVE_SPEECH_MIN_CONFIDENCE


def test_evidence_is_rounded_so_the_boundary_is_not_a_float_accident():
    """0.7 + 0.1 + 0.1 is 0.8999999999999999 in binary floating point, which
    silently failed the >= 0.9 gate the weights were chosen to clear."""
    t = _tracker([
        ({"destination": "Singapore", "intent_strength": 0.7}, 0.7, T),
        ({"destination": "Singapore"}, 0.7, T + 2),
    ])
    assert t.evidence(now=T + 30) == 0.9


def test_still_deciding_stays_quiet():
    """Two live destinations means they have not chosen. Announcing one of them
    is worse than saying nothing."""
    t = _tracker([
        ({"destination": "Singapore", "intent_strength": 0.7}, 0.7, T),
        ({"destination": "Singapore"}, 0.7, T + 2),
        ({"destination": "Bali"}, 0.6, T + 3),
    ])
    assert t.context.destination.contested
    assert t.evidence(now=T + 30) < 0.9


def test_an_unsettled_route_scores_lower_than_a_settled_one():
    steps = [
        ({"destination": "Singapore", "intent_strength": 0.7}, 0.7, T),
        ({"destination": "Singapore"}, 0.7, T + 2),
    ]
    t = _tracker(steps)
    assert t.evidence(now=T + 2.5) < t.evidence(now=T + 30)


def test_no_destination_means_no_evidence():
    assert _tracker([({"intent_strength": 0.99}, 0.99, T)]).evidence(now=T + 30) == 0.0
