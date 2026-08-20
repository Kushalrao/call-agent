"""Speaker-attributed transcript aggregation (spec section 4.2).

The critical property: **attribution is never inferred.** Each STT session is
bound to exactly one participant's audio track, so every fragment is born tagged
with its speaker. The aggregator's only real job is ordering.

Timestamp mechanics, which are the subtle part. Deepgram reports word and
utterance timings as offsets *relative to the start of its own audio stream*, so
two speakers' offsets are not comparable. Each stream therefore records a
`stream_epoch` from a shared monotonic clock at its first frame, and absolute
time is `stream_epoch + offset`. Both streams run in the same process, so those
epochs come from the same clock and the results are directly comparable.

Overlapping speech is expected and preserved as overlapping intervals — never
merged, truncated, or serialised. The speaker label makes it unambiguous.
"""

from __future__ import annotations

import bisect
import time
from dataclasses import dataclass, field
from typing import Iterable

# Below this, a transcript is rendered with an (unclear) marker so a model can
# discount it rather than treating a mishearing as fact.
LOW_CONFIDENCE = 0.5

# Rolling window that feeds the decision layer.
WINDOW_SECONDS = 120.0
WINDOW_UTTERANCES = 40


@dataclass
class Utterance:
    utterance_id: int
    speaker_id: str
    speaker_name: str
    text: str
    t_start: float  # absolute, shared monotonic clock
    t_end: float
    confidence: float

    @property
    def is_unclear(self) -> bool:
        return self.confidence < LOW_CONFIDENCE

    def overlaps(self, other: Utterance) -> bool:
        """Half-open interval intersection. Touching intervals (one ends exactly
        as the next begins) are not an overlap — that is just conversation."""
        return self.t_start < other.t_end and other.t_start < self.t_end


class StreamClock:
    """Converts one STT stream's relative offsets into absolute time.

    A stream that reconnects gets a new epoch. The replayed ring buffer means
    the new stream's audio starts `replayed_seconds` in the past, so the epoch is
    set back by that much — otherwise replayed audio would be stamped as if it
    had just been spoken and would sort after speech that actually followed it.
    """

    def __init__(self, *, now: float | None = None) -> None:
        self._epoch: float | None = None
        self._pending_now = now

    def start(self, *, now: float | None = None, replayed_seconds: float = 0.0) -> float:
        base = now if now is not None else (self._pending_now or time.monotonic())
        self._epoch = base - replayed_seconds
        return self._epoch

    def restart(self, *, now: float | None = None, replayed_seconds: float = 0.0) -> float:
        """After an STT reconnect. Same as start(); named for intent at call sites."""
        return self.start(now=now, replayed_seconds=replayed_seconds)

    @property
    def started(self) -> bool:
        return self._epoch is not None

    def absolute(self, offset: float) -> float:
        if self._epoch is None:
            raise RuntimeError("StreamClock.start() must be called before absolute()")
        return self._epoch + offset


class TranscriptAggregator:
    """The call's transcript: full log, rolling window, and the rendering that
    every model sees."""

    def __init__(
        self,
        *,
        call_started_at: float | None = None,
        window_seconds: float = WINDOW_SECONDS,
        window_utterances: int = WINDOW_UTTERANCES,
    ) -> None:
        self.call_started_at = (
            call_started_at if call_started_at is not None else time.monotonic()
        )
        self.window_seconds = window_seconds
        self.window_utterances = window_utterances

        self._log: list[Utterance] = []
        self._starts: list[float] = []  # parallel to _log, for bisect
        self._next_id = 1

    # --- ingest ------------------------------------------------------------

    def add(
        self,
        *,
        speaker_id: str,
        speaker_name: str,
        text: str,
        t_start: float,
        t_end: float,
        confidence: float = 1.0,
    ) -> Utterance:
        """Insert a finalized utterance, ordered by start time.

        Inserted with bisect rather than appended: utterances arrive per-stream,
        so a slower stream's earlier speech can land after a faster stream's
        later speech. Appending would produce a log that reads out of order.
        """
        utterance = Utterance(
            utterance_id=self._next_id,
            speaker_id=speaker_id,
            speaker_name=speaker_name,
            text=text.strip(),
            t_start=t_start,
            t_end=max(t_end, t_start),
            confidence=confidence,
        )
        self._next_id += 1

        index = bisect.bisect_right(self._starts, t_start)
        self._starts.insert(index, t_start)
        self._log.insert(index, utterance)
        return utterance

    # --- read --------------------------------------------------------------

    @property
    def log(self) -> list[Utterance]:
        """The whole call, in order. Never truncated — the window is only what
        the models are shown."""
        return list(self._log)

    def window(self, *, now: float | None = None) -> list[Utterance]:
        now = now if now is not None else time.monotonic()
        cutoff = now - self.window_seconds
        recent = [u for u in self._log if u.t_end >= cutoff]
        return recent[-self.window_utterances :]

    def render_window(self, *, now: float | None = None) -> str:
        """The exact text every model receives (spec section 4.2).

        Raw unlabeled text never reaches a model — one implementation, so the
        classifier and the reasoning turn can never disagree about who said what.
        """
        return self.render(self.window(now=now))

    def render(self, utterances: Iterable[Utterance]) -> str:
        lines: list[str] = []
        previous: Utterance | None = None
        for u in utterances:
            stamp = self._mmss(u.t_start)
            markers = ""
            if previous is not None and u.overlaps(previous):
                markers += "(overlapping) "
            if u.is_unclear:
                markers += "(unclear) "
            lines.append(f"[{stamp}] {u.speaker_name}: {markers}{u.text}")
            previous = u
        return "\n".join(lines)

    def _mmss(self, absolute: float) -> str:
        elapsed = max(0.0, absolute - self.call_started_at)
        return f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}"

    # --- persistence -------------------------------------------------------

    def to_records(self) -> list[dict]:
        """For persisting the transcript after the call (spec section 9, as
        amended: transcripts ARE kept, 30-day TTL). Times are relative to call
        start so a record is meaningful without the monotonic clock it came
        from."""
        return [
            {
                "utterance_id": u.utterance_id,
                "speaker_id": u.speaker_id,
                "speaker_name": u.speaker_name,
                "text": u.text,
                "t_start_s": round(u.t_start - self.call_started_at, 3),
                "t_end_s": round(u.t_end - self.call_started_at, 3),
                "confidence": round(u.confidence, 3),
            }
            for u in self._log
        ]

    def __len__(self) -> int:
        return len(self._log)
