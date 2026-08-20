"""Worker plumbing that is worth testing without a live call.

The audio path needs LiveKit and Deepgram, so it is exercised by
scripts/rehearse_call.py. What is tested here is everything that turned out to
be wrong on the first live run, so it stays fixed.
"""

from __future__ import annotations

import pytest

from agent.vocabulary import KEYTERMS
from agent.worker import extract_call_id, sanitize_call_id
from control_plane.logging_setup import _safe_segment


# --- call_id extraction -----------------------------------------------------
#
# The first live rehearsal wrote its transcript to a file literally named
# `{"call_id":"rehearse-ef1a5f18"}.transcript.json`: room metadata had not
# arrived yet, and the job-metadata fallback was used as a raw string instead of
# being parsed.


@pytest.mark.parametrize(
    "room,job,expected",
    [
        ('{"call_id":"abc123"}', None, "abc123"),
        # The bug: job metadata is an envelope too, not a bare id.
        (None, '{"call_id":"abc123"}', "abc123"),
        ("", '{"call_id":"abc123"}', "abc123"),
        ("{}", '{"call_id":"xyz"}', "xyz"),
        ('{"call_id":null}', '{"call_id":"fallback"}', "fallback"),
        ("bare-id", None, "bare-id"),
        ("not json {{{", None, "notjson"),
        (None, None, "unknown"),
    ],
)
def test_extract_call_id(room, job, expected):
    assert extract_call_id(room, job) == expected


def test_room_metadata_wins_over_job_metadata():
    """Room metadata is set by the control plane and is authoritative."""
    assert extract_call_id('{"call_id":"from-room"}', '{"call_id":"from-job"}') == "from-room"


# --- filename safety --------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("../../etc/passwd", "etcpasswd"),
        ("a/b", "ab"),
        ("call-123_x", "call-123_x"),
        ("  spaced  ", "spaced"),
        ("...", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_sanitize_call_id(raw, expected):
    assert sanitize_call_id(raw) == expected


def test_call_id_cannot_escape_the_log_directory():
    """call_id becomes a filename in two places; both must constrain it."""
    assert "/" not in _safe_segment("../../etc/passwd")
    assert _safe_segment("../../etc/passwd") == "etcpasswd"
    assert _safe_segment("") == "unnamed"
    assert _safe_segment("...") == "unnamed"


def test_sanitize_bounds_length():
    assert len(sanitize_call_id("x" * 500)) == 128


# --- keyterms ---------------------------------------------------------------


def test_keyterms_include_the_word_that_was_misheard():
    """A rehearsal transcribed "Bali" as "Boli" — a wrong city is a wrong flight
    search, so the destination vocabulary is biased explicitly."""
    assert "Bali" in KEYTERMS
    assert "Bangalore" in KEYTERMS


def test_keyterms_are_unique_and_bounded():
    """Keyterms are a bias: too many and the model hears place names everywhere."""
    assert len(KEYTERMS) == len(set(KEYTERMS)), "duplicate keyterms dilute the bias"
    assert len(KEYTERMS) < 200
