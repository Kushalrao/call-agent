"""Guardrails (spec section 5.2, and the 2026-08-21 proactive-speech amendment).

These tests are the safety net for a decision that removed one: proactive
triggers may now speak, so what stops the agent being intolerable is entirely in
this module.
"""

from __future__ import annotations

from agent.policy import (
    COOLDOWN_SECONDS,
    FLOOR_HOLD_S,
    MAX_PROCESSING_S,
    SHARED_SILENCE_S,
    PROACTIVE_SPEECH_MIN_CONFIDENCE,
    PROACTIVE_WIDGET_MIN_CONFIDENCE,
    VOICE_EXPIRY_S,
    Channel,
    Decision,
    Phase,
    Policy,
    SpeechFloor,
    Trigger,
)

T = 1000.0


def fired(d: Decision) -> tuple[bool, bool, bool]:
    return d.fire, d.may_render, d.may_speak


# --- direct address outranks every budget ----------------------------------


def test_direct_address_bypasses_cooldown_and_limits():
    """Refusing a question someone actually asked, to respect a politeness
    budget, is worse than the interruption the budget exists to prevent."""
    p = Policy()
    p.record_action(trigger=Trigger.FLIGHT_INTENT,
                    channels=frozenset({Channel.WIDGET, Channel.SPEECH}), now=T)
    assert fired(p.evaluate(Trigger.DIRECT_ADDRESS, now=T + 1)) == (True, True, True)


def test_widget_tap_is_treated_as_direct_address():
    p = Policy()
    p._last_action_at = T
    assert p.evaluate(Trigger.WIDGET_TAP, now=T + 1).fire


def test_direct_address_spends_no_proactive_budget():
    p = Policy()
    d = p.evaluate(Trigger.DIRECT_ADDRESS, now=T)
    p.record_action(trigger=Trigger.DIRECT_ADDRESS, channels=d.channels, now=T)
    # Cooldown still applies to the *next* proactive attempt...
    assert not p.evaluate(Trigger.FLIGHT_INTENT, confidence=1.0, now=T + 5).fire
    # ...but the proactive budgets were never touched.
    later = p.evaluate(Trigger.FLIGHT_INTENT, confidence=1.0, now=T + COOLDOWN_SECONDS + 1)
    assert fired(later) == (True, True, True)


# --- cooldown ---------------------------------------------------------------


def test_proactive_is_refused_during_cooldown():
    p = Policy()
    p.record_action(trigger=Trigger.FLIGHT_INTENT, channels=frozenset({Channel.WIDGET}), now=T)
    d = p.evaluate(Trigger.FLIGHT_INTENT, confidence=1.0, now=T + 5)
    assert not d.fire and d.reason.startswith("cooldown")


def test_cooldown_expires():
    """Isolated on a *direct* action, because that is the only case where the
    20s cooldown is the binding constraint — see the test below."""
    p = Policy()
    p.record_action(trigger=Trigger.DIRECT_ADDRESS,
                    channels=frozenset({Channel.WIDGET, Channel.SPEECH}), now=T)
    assert not p.evaluate(Trigger.FLIGHT_INTENT, confidence=1.0, now=T + 5).fire
    assert p.evaluate(Trigger.FLIGHT_INTENT, confidence=1.0,
                      now=T + COOLDOWN_SECONDS + 0.1).fire


def test_the_tighter_of_cooldown_and_rate_limit_wins():
    """Worth stating explicitly because it is easy to misread the spec as
    "proactive actions are 20s apart". They are not: after a proactive action the
    120s budget dominates and the 20s cooldown never binds. The cooldown's real
    job is spacing a proactive action after a *direct* one."""
    p = Policy()
    d = p.evaluate(Trigger.FLIGHT_INTENT, confidence=0.99, now=T)
    p.record_action(trigger=Trigger.FLIGHT_INTENT, channels=d.channels, now=T)

    past_cooldown = p.evaluate(Trigger.FLIGHT_INTENT, confidence=0.99,
                               now=T + COOLDOWN_SECONDS + 1)
    assert not past_cooldown.fire
    assert past_cooldown.reason == "rate_limit:proactive_widget", (
        "cooldown expired but the 2-minute budget still holds"
    )


def test_a_busy_agent_does_not_start_again():
    for phase in (Phase.PROCESSING, Phase.RESPONDING):
        p = Policy(phase=phase)
        d = p.evaluate(Trigger.DIRECT_ADDRESS, now=T)
        assert not d.fire and d.reason == f"busy:{phase.value}"


# --- the asymmetry: widgets tolerate uncertainty, speech does not ----------


def test_confidence_below_the_widget_bar_fires_nothing():
    p = Policy()
    d = p.evaluate(Trigger.FLIGHT_INTENT,
                   confidence=PROACTIVE_WIDGET_MIN_CONFIDENCE - 0.01, now=T)
    assert not d.fire and d.reason.startswith("low_confidence")


def test_middling_confidence_renders_but_stays_silent():
    """The gate that makes proactive speech safe: a wrong card is ignorable, a
    wrong remark is not."""
    p = Policy()
    d = p.evaluate(Trigger.FLIGHT_INTENT,
                   confidence=(PROACTIVE_WIDGET_MIN_CONFIDENCE
                               + PROACTIVE_SPEECH_MIN_CONFIDENCE) / 2, now=T)
    assert fired(d) == (True, True, False)
    assert d.reason == "proactive:widget_only"


def test_high_confidence_speaks_and_renders():
    p = Policy()
    d = p.evaluate(Trigger.FLIGHT_INTENT, confidence=0.99, now=T)
    assert fired(d) == (True, True, True)


def test_speech_bar_is_strictly_higher_than_the_widget_bar():
    assert PROACTIVE_SPEECH_MIN_CONFIDENCE > PROACTIVE_WIDGET_MIN_CONFIDENCE


# --- separate budgets ------------------------------------------------------


def test_speech_and_widget_budgets_are_separate():
    """A silent widget must not spend the quota a spoken answer needed."""
    p = Policy()
    # Spend only the widget budget.
    p.record_action(trigger=Trigger.FLIGHT_INTENT, channels=frozenset({Channel.WIDGET}), now=T)
    snap = p.snapshot(now=T)
    assert snap["proactive_widgets"] == 1
    assert snap["proactive_speech"] == 0


def test_proactive_speech_is_capped_within_the_window():
    p = Policy()
    d = p.evaluate(Trigger.FLIGHT_INTENT, confidence=0.99, now=T)
    p.record_action(trigger=Trigger.FLIGHT_INTENT, channels=d.channels, now=T)
    # Past cooldown but inside the 2-minute window.
    d2 = p.evaluate(Trigger.FLIGHT_INTENT, confidence=0.99, now=T + 30)
    assert not d2.fire and d2.reason == "rate_limit:proactive_widget"


def test_budgets_are_released_after_the_window():
    p = Policy()
    d = p.evaluate(Trigger.FLIGHT_INTENT, confidence=0.99, now=T)
    p.record_action(trigger=Trigger.FLIGHT_INTENT, channels=d.channels, now=T)
    assert p.evaluate(Trigger.FLIGHT_INTENT, confidence=0.99, now=T + 121).fire


def test_evaluate_spends_nothing():
    """A trigger evaluated and then dropped upstream must not silently consume
    the slot the next real opportunity needs."""
    p = Policy()
    for i in range(5):
        assert p.evaluate(Trigger.FLIGHT_INTENT, confidence=0.99, now=T + i).fire
    assert p.snapshot(now=T)["proactive_speech"] == 0


# --- the floor -------------------------------------------------------------


def test_floor_is_busy_while_someone_speaks():
    f = SpeechFloor()
    f.speech_started("u-rohan", now=T)
    assert not f.is_free(now=T + 0.5)
    # Continuous talking keeps it held, as long as evidence keeps arriving.
    for t in (1.0, 2.0, 3.0, 4.0):
        f.refresh("u-rohan", now=T + t)
        assert not f.is_free(now=T + t)


def test_floor_needs_shared_silence_not_just_one_person_stopping():
    f = SpeechFloor()
    f.speech_started("u-rohan", now=T)
    f.speech_started("u-kushal", now=T)
    assert f.is_overlapping(now=T)
    f.speech_ended("u-rohan", now=T + 1)
    assert f.is_busy(now=T + 1), "Kushal is still talking"
    f.speech_ended("u-kushal", now=T + 2)
    assert not f.is_free(now=T + 2.3), "0.3s is not enough"
    assert f.is_free(now=T + 3.0)


def test_a_missing_end_event_cannot_wedge_the_floor():
    """The bug this cost us: a live call had a finished search in hand and dropped
    it with `floor_never_opened`, because START_OF_SPEECH arrived without its
    matching END_OF_SPEECH and the speaker stayed marked as talking forever.
    Waiting politely for silence that can never come is worse than interrupting."""
    f = SpeechFloor()
    f.speech_started("u-rohan", now=T)   # ...and no matching speech_ended, ever
    assert not f.is_free(now=T + 1.0), "still held while the hold is fresh"
    assert f.is_free(now=T + FLOOR_HOLD_S + SHARED_SILENCE_S + 0.1), (
        "a stale hold must lapse on its own"
    )


def test_a_lapsed_hold_does_not_reset_the_silence_timer():
    """The hold lapses as of when it went stale, not when we noticed — otherwise
    every check would push the shared-silence window further out."""
    f = SpeechFloor()
    f.speech_started("u-rohan", now=T)
    assert f.silent_for(now=T + FLOOR_HOLD_S + 5.0) >= 5.0


def test_refresh_does_not_create_a_hold():
    """A finalized utterance arrives well after the speech it describes, so
    treating it as "speaking now" would make the floor busy at the worst moment."""
    f = SpeechFloor()
    f.refresh("u-rohan", now=T)
    assert f.is_free(now=T)


def test_floor_is_free_before_anyone_has_spoken():
    assert SpeechFloor().is_free(now=T)


# --- hold and expire (spec section 8) -------------------------------------


def test_unprompted_speech_is_dropped_rather_than_said_late():
    """Answering the question from eight seconds ago is worse than silence; the
    widget already carried the answer."""
    p = Policy()
    # Rohan talks continuously through the whole window.
    for t in range(0, int(VOICE_EXPIRY_S) + 2):
        p.floor.speech_started("u-rohan", now=T + t)
    d = p.may_speak_now(held_since=T, direct=False, now=T + VOICE_EXPIRY_S + 0.1)
    assert not d.fire and d.reason == "voice_expired"


def test_a_direct_answer_keeps_waiting_past_the_expiry():
    """Someone asked; the answer is still wanted."""
    p = Policy()
    for t in range(0, int(VOICE_EXPIRY_S) + 12):
        p.floor.speech_started("u-rohan", now=T + t)
    d = p.may_speak_now(held_since=T, direct=True, now=T + VOICE_EXPIRY_S + 10)
    assert not d.fire and d.reason == "floor_busy"


def test_speech_goes_as_soon_as_the_floor_opens():
    p = Policy()
    p.floor.speech_started("u-rohan", now=T)
    p.floor.speech_ended("u-rohan", now=T + 1)
    assert p.may_speak_now(held_since=T, direct=False, now=T + 2).fire


def test_expiry_is_measured_from_readiness_not_from_the_trigger():
    """A slow model must not eat the social window."""
    p = Policy()
    ready_at = T + 30  # synthesis finished late
    for t in range(0, 40):  # someone is talking throughout
        p.floor.speech_started("u-rohan", now=T + t)
    assert p.may_speak_now(held_since=ready_at, direct=False,
                           now=ready_at + 1).reason == "floor_busy"
    assert p.may_speak_now(held_since=ready_at, direct=False,
                           now=ready_at + VOICE_EXPIRY_S + 1).reason == "voice_expired"


# --- barge-in -------------------------------------------------------------


def test_barge_in_returns_to_listening_without_starting_a_cooldown():
    """Being interrupted is not the agent having had its turn."""
    p = Policy(phase=Phase.RESPONDING)
    p.barge_in()
    assert p.phase is Phase.LISTENING
    assert p.cooldown_remaining(now=T) == 0.0
    assert p.evaluate(Trigger.FLIGHT_INTENT, confidence=0.99, now=T).fire


# --- the gap a live run exposed --------------------------------------------


def test_consecutive_proactive_fires_are_impossible_once_recorded():
    """A live call fired four proactive triggers in twenty seconds because the
    firing path never called record_action(). The limits passed in tests and were
    inert in production. This is that scenario, end to end."""
    p = Policy()
    fires = []
    for i in range(4):
        d = p.evaluate(Trigger.FLIGHT_INTENT, confidence=0.92, now=T + i * 5)
        if d.fire:
            p.record_action(trigger=Trigger.FLIGHT_INTENT, channels=d.channels,
                            now=T + i * 5)
        fires.append(d.fire)
    assert fires == [True, False, False, False], (
        "only the first proactive trigger in a 2-minute window may act"
    )


def test_recording_a_fire_engages_the_cooldown_for_direct_address_too():
    p = Policy()
    d = p.evaluate(Trigger.DIRECT_ADDRESS, now=T)
    p.record_action(trigger=Trigger.DIRECT_ADDRESS, channels=d.channels, now=T)
    assert p.cooldown_remaining(now=T + 1) > 0
    # But a second direct question still gets through — someone asked.
    assert p.evaluate(Trigger.DIRECT_ADDRESS, now=T + 1).fire


def test_a_fired_trigger_holds_the_turn():
    """The phase guard was decorative: a live call fired three direct_address
    triggers in six seconds — one question plus two added constraints — because
    nothing ever left LISTENING. The agent would have answered three times."""
    p = Policy()
    d = p.evaluate(Trigger.DIRECT_ADDRESS, now=T)
    assert d.fire
    p.begin(Trigger.DIRECT_ADDRESS, now=T)

    # The follow-up constraints must join the turn already underway.
    for offset in (2, 4):
        follow_up = p.evaluate(Trigger.DIRECT_ADDRESS, now=T + offset)
        assert not follow_up.fire
        assert follow_up.reason == "busy:processing"

    p.complete(now=T + 6)
    assert p.evaluate(Trigger.DIRECT_ADDRESS, now=T + 7).fire


def test_a_claimed_turn_blocks_briefly_then_gives_the_floor_back():
    """Both halves of a lesson learned twice.

    An unmatched begin() used to deafen the agent for the rest of the call. Then,
    with begin/complete correct, a 45s throttled flight search still refused five
    consecutive requests as busy:processing — the turn was legitimately held, just
    for far too long. Being asked twice is a much smaller failure than ignoring
    someone, so an overrunning turn now yields."""
    p = Policy()
    p.begin(Trigger.DIRECT_ADDRESS, now=T)

    # Briefly busy, which is correct: a follow-up joins the turn underway.
    assert p.evaluate(Trigger.DIRECT_ADDRESS, now=T + 5).reason == "busy:processing"

    # Past the cap it yields, even with no complete() at all.
    assert p.evaluate(Trigger.DIRECT_ADDRESS, now=T + MAX_PROCESSING_S + 1).fire


def test_an_overrun_turn_yields_without_needing_complete():
    """The guarantee that matters: no code path, however broken, can leave the
    agent permanently unable to hear a direct request."""
    p = Policy()
    p.begin(Trigger.DIRECT_ADDRESS, now=T)
    for offset in (60, 600, 6000):
        assert p.evaluate(Trigger.DIRECT_ADDRESS, now=T + offset).fire
