"""Structured logging, from line one (spec §11.1).

Every log line is one JSON object. `call_id` is minted by the control plane and
propagated everywhere — LiveKit room metadata carries it so the agent worker
picks it up at job start. `utterance_id` correlates a single utterance across
its whole journey.

Two sinks:
  - stdout (pretty in dev, raw JSON in prod)
  - logs/{call_id}.jsonl — the primary debugging artifact, read by
    scripts/call_report.py

Privacy split: level=info carries metadata only. Anything containing utterance
text must be passed as `text=...` and is dropped unless LOG_TRANSCRIPTS=true.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .config import get_settings

SERVICE = os.environ.get("LOG_SERVICE", "control-plane")
_lock = threading.Lock()


class Events:
    """Canonical event names (spec §11.1). Grep-able and exhaustive.

    Add to this class rather than passing ad-hoc strings — the whole point is
    that `grep trigger.fired` finds every occurrence.
    """

    # call lifecycle
    CALL_CREATED = "call.created"
    CALL_RINGING = "call.ringing"
    CALL_ACCEPTED = "call.accepted"
    CALL_DECLINED = "call.declined"
    CALL_TIMEOUT = "call.timeout"
    CALL_PARTICIPANT_JOINED = "call.participant_joined"
    CALL_ACTIVE = "call.active"
    CALL_ENDED = "call.ended"

    # agent lifecycle
    AGENT_DISPATCHED = "agent.dispatched"
    AGENT_JOINED = "agent.joined"
    AGENT_REMOVED = "agent.removed"

    # auth / transport
    AUTH_DEV_LOGIN = "auth.dev_login"
    TOKEN_MINTED = "token.minted"
    WS_CONNECTED = "ws.connected"
    WS_DISCONNECTED = "ws.disconnected"
    WS_DELIVERED = "ws.delivered"

    # agent worker: STT + aggregation (spec 11.1)
    TRACK_SUBSCRIBED = "track.subscribed"
    STT_SESSION_OPEN = "stt.session_open"
    STT_INTERIM = "stt.interim"
    STT_FINAL = "stt.final"
    STT_RECONNECT = "stt.reconnect"
    STT_DEGRADED = "stt.degraded"
    AGGREGATOR_UTTERANCE = "aggregator.utterance"
    CLASSIFIER_REQUEST = "classifier.request"
    CLASSIFIER_RESULT = "classifier.result"
    TRIGGER_FIRED = "trigger.fired"
    TRIGGER_SUPPRESSED = "trigger.suppressed"
    LLM_TURN_START = "llm.turn_start"
    LLM_TOOL_CALL = "llm.tool_call"
    LLM_TURN_END = "llm.turn_end"
    WIDGET_PUBLISHED = "widget.published"
    TTS_START = "tts.start"
    TTS_FIRST_BYTE = "tts.first_byte"
    TTS_CANCELLED = "tts.cancelled"

    # failures
    ERROR_LIVEKIT = "error.livekit"
    ERROR_RATE_LIMITED = "error.rate_limited"
    ERROR_UNAUTHORIZED = "error.unauthorized"
    ERROR_INTERNAL = "error.internal"


def _safe_segment(value: str) -> str:
    """call_id becomes a filename, so it is constrained to one path segment.

    Every call_id we mint is a uuid, but this function is the last line before a
    filesystem write and it does not get to assume that — one bad `..` in room
    metadata should not be able to steer a log write out of the log directory.
    """
    cleaned = "".join(c for c in value if c.isalnum() or c in "-_")
    return cleaned[:128] or "unnamed"


def _call_log_path(call_id: str) -> Path:
    settings = get_settings()
    d = Path(settings.log_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_safe_segment(call_id)}.jsonl"


def log_event(
    event: str,
    *,
    call_id: str | None = None,
    level: str = "info",
    text: str | None = None,
    **fields: Any,
) -> None:
    """Emit one structured event.

    `text` is the privacy-gated channel: pass anything derived from what people
    actually said here, never in **fields.
    """
    settings = get_settings()

    record: dict[str, Any] = {
        "ts": time.time(),
        "level": level,
        "service": SERVICE,
        "event": event,
    }
    if call_id:
        record["call_id"] = call_id
    record.update({k: v for k, v in fields.items() if v is not None})

    if text is not None and settings.log_transcripts:
        record["text"] = text

    line = json.dumps(record, default=str, sort_keys=True)

    with _lock:
        if settings.log_pretty:
            extras = " ".join(
                f"{k}={v}"
                for k, v in record.items()
                if k not in {"ts", "level", "service", "event"}
            )
            print(f"[{level:<5}] {event:<28} {extras}", file=sys.stderr, flush=True)
        else:
            print(line, file=sys.stderr, flush=True)

        # Per-call JSONL. Opened per write: dev-scale volume, and it means a
        # crash never loses buffered lines.
        if call_id:
            try:
                with _call_log_path(call_id).open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:  # never let logging break a call
                print(f"[error] log sink failed: {exc}", file=sys.stderr, flush=True)


class Timer:
    """Measures a stage so every event can carry its own latency_ms (spec §11.1).

    with Timer() as t:
        ...
    log_event(Events.TOKEN_MINTED, latency_ms=t.ms)
    """

    def __init__(self) -> None:
        self._start = 0.0
        self.ms = 0.0

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.ms = round((time.perf_counter() - self._start) * 1000, 2)
