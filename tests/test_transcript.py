"""Transcript aggregator — the tests the spec requires (section 4.2).

All pure data: no audio, no network, no LiveKit. These are the cheapest place to
catch the bugs that would otherwise show up as "the agent misunderstood who said
what" on a live call.
"""

from __future__ import annotations

from agent.transcript import (
    LOW_CONFIDENCE,
    StreamClock,
    TranscriptAggregator,
    Utterance,
)

T0 = 1000.0  # arbitrary monotonic origin


def agg() -> TranscriptAggregator:
    return TranscriptAggregator(call_started_at=T0)


def say(a, speaker, text, start, end, confidence=0.95):
    name = {"u-kushal": "Kushal", "u-rohan": "Rohan"}.get(speaker, speaker)
    return a.add(
        speaker_id=speaker, speaker_name=name, text=text,
        t_start=T0 + start, t_end=T0 + end, confidence=confidence,
    )


# --- ordering ---------------------------------------------------------------


def test_interleaved_ordering_across_two_streams():
    """Two speakers, two STT streams. A slower stream can deliver earlier speech
    after a faster stream delivered later speech — the log must still read in
    the order things were actually said."""
    a = agg()
    say(a, "u-rohan", "december is packed at work man", 38, 41)
    say(a, "u-kushal", "and direct flights only", 44, 46)
    # Arrives late, but was spoken earlier than the line above.
    say(a, "u-kushal", "what about second week of December instead", 41, 44)

    assert [u.text for u in a.log] == [
        "december is packed at work man",
        "what about second week of December instead",
        "and direct flights only",
    ]


def test_utterance_ids_reflect_arrival_not_order():
    """ids are assigned on arrival so they can correlate an utterance across the
    pipeline (spec section 11.1); ordering is by time, separately."""
    a = agg()
    late = say(a, "u-kushal", "spoken first, arrived second", 10, 12)
    early = say(a, "u-rohan", "spoken second, arrived first", 20, 22)
    assert late.utterance_id == 1 and early.utterance_id == 2
    assert [u.utterance_id for u in a.log] == [1, 2]

    later = say(a, "u-kushal", "spoken in the middle", 15, 16)
    assert later.utterance_id == 3
    assert [u.text for u in a.log][1] == "spoken in the middle"


# --- overlap ----------------------------------------------------------------


def test_heavy_overlap_is_preserved_never_merged():
    """People talk over each other. Both utterances survive intact, with the
    overlap annotated — merging or truncating would lose what was said."""
    a = agg()
    say(a, "u-rohan", "yeah that works but keep it under 30k each", 44, 48)
    say(a, "u-kushal", "and direct flights only", 45, 47)

    assert len(a.log) == 2
    rendered = a.render_window(now=T0 + 50)
    assert "(overlapping)" in rendered
    assert "under 30k each" in rendered
    assert "direct flights only" in rendered


def test_touching_intervals_are_not_overlap():
    """One person finishing exactly as the other starts is normal conversation,
    not overlapping speech."""
    a = agg()
    say(a, "u-rohan", "so december then", 10, 12)
    say(a, "u-kushal", "december works", 12, 14)
    assert "(overlapping)" not in a.render_window(now=T0 + 15)


def test_three_way_overlap_annotates_each_against_its_predecessor():
    a = agg()
    say(a, "u-rohan", "one", 10, 20)
    say(a, "u-kushal", "two", 11, 21)
    say(a, "u-rohan", "three", 12, 22)
    lines = a.render_window(now=T0 + 25).splitlines()
    assert "(overlapping)" not in lines[0]
    assert "(overlapping)" in lines[1]
    assert "(overlapping)" in lines[2]


# --- stream clock / reconnect ------------------------------------------------


def test_stream_clock_makes_two_streams_comparable():
    """Deepgram offsets are relative to each stream's own start, so absolute
    times must come from a shared clock or speakers cannot be ordered."""
    a_clock, b_clock = StreamClock(), StreamClock()
    a_clock.start(now=T0 + 0.0)
    b_clock.start(now=T0 + 5.0)  # second speaker's stream opened 5s later

    # Both report "2.0s into my stream" — different moments in the call.
    assert a_clock.absolute(2.0) == T0 + 2.0
    assert b_clock.absolute(2.0) == T0 + 7.0


def test_reconnect_with_replay_does_not_stamp_old_audio_as_new():
    """After an STT socket drops, the ring buffer is replayed into a fresh
    stream. Without backdating the epoch by the replayed duration, that audio
    would be stamped as just-spoken and would sort after speech that actually
    came later."""
    clock = StreamClock()
    clock.start(now=T0)
    before = clock.absolute(10.0)  # speech at t+10

    # Socket dies at t+12; 4s of buffered audio is replayed into a new stream.
    clock.restart(now=T0 + 12.0, replayed_seconds=4.0)
    replayed = clock.absolute(0.0)  # first replayed frame

    assert replayed == T0 + 8.0, "replayed audio must be backdated"
    assert replayed < T0 + 12.0
    assert before < T0 + 12.0

    a = agg()
    say(a, "u-rohan", "earlier speech", 10, 11)
    a.add(speaker_id="u-rohan", speaker_name="Rohan", text="replayed speech",
          t_start=replayed, t_end=replayed + 1, confidence=0.9)
    say(a, "u-kushal", "after the drop", 13, 14)
    assert [u.text for u in a.log] == ["replayed speech", "earlier speech", "after the drop"]


def test_clock_requires_start():
    clock = StreamClock()
    assert clock.started is False
    try:
        clock.absolute(1.0)
    except RuntimeError as exc:
        assert "start()" in str(exc)
    else:
        raise AssertionError("absolute() should refuse before start()")


# --- rendering --------------------------------------------------------------


def test_render_matches_the_documented_format():
    """This exact rendering feeds both the classifier and the reasoning turn, so
    it is golden-tested (spec section 4.2)."""
    a = agg()
    say(a, "u-rohan", "december is packed at work man", 38, 41)
    say(a, "u-kushal", "what about second week of December instead", 41, 44)
    say(a, "u-rohan", "yeah that works but keep it under 30k each", 44, 47)
    say(a, "u-kushal", "and direct flights only", 45, 47)

    assert a.render_window(now=T0 + 50) == (
        "[00:38] Rohan: december is packed at work man\n"
        "[00:41] Kushal: what about second week of December instead\n"
        "[00:44] Rohan: yeah that works but keep it under 30k each\n"
        "[00:45] Kushal: (overlapping) and direct flights only"
    )


def test_low_confidence_is_marked_so_a_model_can_discount_it():
    a = agg()
    say(a, "u-rohan", "somewhere in goa maybe", 10, 12, confidence=0.31)
    say(a, "u-kushal", "goa sounds good", 13, 15, confidence=0.97)
    lines = a.render_window(now=T0 + 20).splitlines()
    assert "(unclear)" in lines[0]
    assert "(unclear)" not in lines[1]


def test_timestamps_are_relative_to_call_start():
    a = TranscriptAggregator(call_started_at=T0)
    say(a, "u-rohan", "much later", 3725, 3726)  # 62m 05s
    assert a.render_window(now=T0 + 3730).startswith("[62:05]")


def test_both_markers_can_apply_to_one_utterance():
    a = agg()
    say(a, "u-rohan", "clear line", 10, 20)
    say(a, "u-kushal", "mumbled over the top", 11, 21, confidence=0.2)
    second = a.render_window(now=T0 + 25).splitlines()[1]
    assert "(overlapping)" in second and "(unclear)" in second


# --- window -----------------------------------------------------------------


def test_window_evicts_by_age():
    a = TranscriptAggregator(call_started_at=T0, window_seconds=120)
    say(a, "u-rohan", "ancient history", 10, 12)
    say(a, "u-kushal", "recent", 200, 202)
    window = a.window(now=T0 + 205)
    assert [u.text for u in window] == ["recent"]
    # The full log keeps everything; only the model's view is windowed.
    assert len(a.log) == 2


def test_window_evicts_by_count():
    a = TranscriptAggregator(call_started_at=T0, window_utterances=5)
    for i in range(12):
        say(a, "u-rohan", f"line {i}", i, i + 0.5)
    window = a.window(now=T0 + 13)
    assert len(window) == 5
    assert [u.text for u in window] == [f"line {i}" for i in range(7, 12)]


def test_window_keeps_an_utterance_still_in_progress_at_the_boundary():
    """Eviction is on t_end, so a long utterance that started before the cutoff
    but is still recent stays visible."""
    a = TranscriptAggregator(call_started_at=T0, window_seconds=60)
    say(a, "u-rohan", "long rambling explanation", 100, 175)
    assert [u.text for u in a.window(now=T0 + 180)] == ["long rambling explanation"]


def test_empty_transcript_renders_empty_not_error():
    assert agg().render_window(now=T0) == ""


# --- persistence ------------------------------------------------------------


def test_records_are_portable_without_the_monotonic_clock():
    a = agg()
    say(a, "u-rohan", "first", 10, 12, confidence=0.9)
    say(a, "u-kushal", "second", 13, 15)
    records = a.to_records()
    assert records[0]["t_start_s"] == 10.0
    assert records[0]["speaker_name"] == "Rohan"
    assert records[0]["confidence"] == 0.9
    assert records[1]["t_start_s"] == 13.0
    # Absolute monotonic values must not leak — they mean nothing after restart.
    assert all(r["t_start_s"] < 1000 for r in records)


def test_end_before_start_is_clamped():
    """STT occasionally reports an end before the start. Clamping keeps interval
    logic (overlap detection, window eviction) sane."""
    a = agg()
    u = a.add(speaker_id="u-rohan", speaker_name="Rohan", text="glitch",
              t_start=T0 + 10, t_end=T0 + 9, confidence=0.9)
    assert u.t_end == u.t_start


def test_text_is_stripped():
    a = agg()
    u = say(a, "u-rohan", "  padded text \n", 10, 11)
    assert u.text == "padded text"


def test_low_confidence_threshold_boundary():
    a = agg()
    at = a.add(speaker_id="x", speaker_name="X", text="at threshold",
               t_start=T0, t_end=T0 + 1, confidence=LOW_CONFIDENCE)
    below = a.add(speaker_id="x", speaker_name="X", text="below",
                  t_start=T0 + 2, t_end=T0 + 3, confidence=LOW_CONFIDENCE - 0.01)
    assert at.is_unclear is False
    assert below.is_unclear is True
