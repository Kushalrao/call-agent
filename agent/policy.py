"""When the agent is allowed to act, and through which channel (spec section 5.2).

This module is the whole reason an ambient agent is tolerable. It holds no
intelligence — it decides nothing about *what* to say — it only decides whether
acting right now is socially acceptable.

It matters more than it did. Under the original spec a proactive trigger could
only render a widget, never speak, which made proactive misfires harmless: a card
you ignore. The 2026-08-21 product decision reversed that, so a proactive misfire
now interrupts two people to say something possibly wrong. Voice cannot be
un-said. The rate limits, the floor check and the confidence gate below are what
replaced that guarantee, and they are not optional.

The asymmetry running through all of it: **widgets tolerate uncertainty, speech
does not.** Rendering is cheap and ignorable, so it gets the lower confidence
bar and the looser budget. Speaking gets the higher bar, its own separate budget,
and a hard expiry.

Design note: `evaluate()` is a pure query and `record_action()` is the only thing
that spends budget. Folding them together would consume a proactive slot for a
trigger that was evaluated and then dropped upstream — and the next real
opportunity would be silently refused.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

# --- spec section 5.2 constants ---------------------------------------------

COOLDOWN_SECONDS = 20.0
PROACTIVE_WIDGET_WINDOW_S = 120.0
PROACTIVE_WIDGET_MAX = 1
# Speech gets its own budget rather than sharing the widget one: rendering is
# cheap, interrupting is not, and sharing would let a silent widget spend the
# quota that a genuinely useful spoken answer needed.
PROACTIVE_SPEECH_WINDOW_S = 120.0
PROACTIVE_SPEECH_MAX = 1

# Spec section 5.1b fires a proactive widget at >= 0.85. Speech is held to a
# higher bar because a wrong card is ignorable and a wrong remark is not.
PROACTIVE_WIDGET_MIN_CONFIDENCE = 0.85
PROACTIVE_SPEECH_MIN_CONFIDENCE = 0.90

# Don't talk over people: the floor is free after this much shared silence.
SHARED_SILENCE_S = 0.7
# Hold-and-expire (spec section 8): an unprompted remark that never found a gap
# is dropped rather than said late. The widget already carried the answer.
VOICE_EXPIRY_S = 5.0
# Longest a single turn may hold the floor before new triggers are allowed again.
MAX_PROCESSING_S = 15.0


class Phase(str, Enum):
    LISTENING = "listening"
    PROCESSING = "processing"
    RESPONDING = "responding"
    COOLDOWN = "cooldown"


class Trigger(str, Enum):
    DIRECT_ADDRESS = "direct_address"   # wake word, or classifier-confirmed
    WIDGET_TAP = "widget_tap"           # treated as direct address (spec 5.1b.3)
    FLIGHT_INTENT = "flight_intent"     # proactive; nobody asked

    @property
    def is_direct(self) -> bool:
        return self in (Trigger.DIRECT_ADDRESS, Trigger.WIDGET_TAP)


class Channel(str, Enum):
    WIDGET = "widget"
    SPEECH = "speech"


@dataclass(frozen=True)
class Decision:
    """Whether to act, and how far. `reason` is logged verbatim as
    trigger.suppressed(reason=...) so a quiet agent can always be explained."""

    fire: bool
    channels: frozenset[Channel] = frozenset()
    reason: str = ""

    @property
    def may_speak(self) -> bool:
        return Channel.SPEECH in self.channels

    @property
    def may_render(self) -> bool:
        return Channel.WIDGET in self.channels


# A speaker is only credited with holding the floor for this long after the last
# evidence they were talking. See the note in SpeechFloor.
FLOOR_HOLD_S = 2.5


class SpeechFloor:
    """Who is talking, so the agent can avoid talking over them.

    Driven by per-track speech-start and speech-end events. Only tracks humans;
    the agent's own speech is handled by barge-in, not by the floor.

    **A held speaker expires.** The floor cannot be allowed to wedge, and it did:
    on a live call the agent had an answer in hand and dropped it with
    `floor_never_opened`, because a `START_OF_SPEECH` arrived without its matching
    `END_OF_SPEECH` and that speaker stayed marked as talking forever. Waiting
    politely for silence that can never arrive is worse than interrupting.

    So the floor is held by *recent evidence of speech*, not by a latch. A missing
    end event self-heals after FLOOR_HOLD_S, and every `refresh()` from ongoing
    speech extends the hold — which means continuous talking still keeps the floor
    busy, exactly as intended, while a lost event cannot silence the agent for the
    rest of the call.
    """

    def __init__(self) -> None:
        # speaker_id -> when we last had evidence they were speaking.
        self._speaking: dict[str, float] = {}
        self._last_release: float | None = None

    def _now(self, now: float | None) -> float:
        return now if now is not None else time.monotonic()

    def _prune(self, now: float) -> None:
        stale = {s: at for s, at in self._speaking.items() if now - at > FLOOR_HOLD_S}
        for speaker in stale:
            del self._speaking[speaker]
        if stale and not self._speaking:
            # The hold lapsed FLOOR_HOLD_S after the last evidence of speech, not
            # at the moment we happened to look. Using the check time instead
            # would push the shared-silence window forward on every poll, so a
            # stale speaker would keep the floor busy forever — the same wedge
            # this expiry exists to prevent, one level up.
            lapsed_at = max(stale.values()) + FLOOR_HOLD_S
            if self._last_release is None or lapsed_at > self._last_release:
                self._last_release = lapsed_at

    def speech_started(self, speaker_id: str, *, now: float | None = None) -> None:
        self._speaking[speaker_id] = self._now(now)

    def refresh(self, speaker_id: str, *, now: float | None = None) -> None:
        """More evidence of ongoing speech. Extends the hold if it is still held.

        Deliberately does not *create* a hold: a finalized utterance arrives well
        after the speech it describes, so treating it as "speaking now" would
        make the floor busy at exactly the wrong moment.
        """
        at = self._now(now)
        self._prune(at)
        if speaker_id in self._speaking:
            self._speaking[speaker_id] = at

    def speech_ended(self, speaker_id: str, *, now: float | None = None) -> None:
        at = self._now(now)
        if speaker_id in self._speaking:
            del self._speaking[speaker_id]
            if not self._speaking:
                self._last_release = at

    def is_busy(self, *, now: float | None = None) -> bool:
        self._prune(self._now(now))
        return bool(self._speaking)

    def is_overlapping(self, *, now: float | None = None) -> bool:
        """Both humans talking at once — the worst moment to interject."""
        self._prune(self._now(now))
        return len(self._speaking) > 1

    # Reading wall time inside these was a real bug: every other method here
    # takes an injectable `now`, so these two silently disagreed with the rest
    # under test and would have disagreed with a replay harness too.
    @property
    def busy(self) -> bool:
        return self.is_busy()

    @property
    def overlapping(self) -> bool:
        return self.is_overlapping()

    def silent_for(self, *, now: float | None = None) -> float:
        """Seconds of shared silence. 0.0 while anyone is speaking.

        Returns +inf when nobody has spoken yet, so an agent that somehow needs
        to speak before any human does is not blocked by a floor never held.
        """
        at = self._now(now)
        self._prune(at)
        if self._speaking:
            return 0.0
        if self._last_release is None:
            return float("inf")
        return at - self._last_release

    def is_free(self, *, now: float | None = None) -> bool:
        return self.silent_for(now=now) >= SHARED_SILENCE_S


@dataclass
class Policy:
    """Per-call guardrail state."""

    phase: Phase = Phase.LISTENING
    floor: SpeechFloor = field(default_factory=SpeechFloor)

    _last_action_at: float | None = None
    _processing_since: float | None = None
    _proactive_widgets: list[float] = field(default_factory=list)
    _proactive_speech: list[float] = field(default_factory=list)

    # --- queries -----------------------------------------------------------

    def cooldown_remaining(self, *, now: float | None = None) -> float:
        if self._last_action_at is None:
            return 0.0
        now = now if now is not None else time.monotonic()
        return max(0.0, COOLDOWN_SECONDS - (now - self._last_action_at))

    def _recent(self, stamps: list[float], window: float, now: float) -> int:
        cutoff = now - window
        stamps[:] = [t for t in stamps if t >= cutoff]
        return len(stamps)

    def evaluate(
        self,
        trigger: Trigger,
        *,
        confidence: float = 1.0,
        now: float | None = None,
    ) -> Decision:
        """Pure. Spends nothing — call record_action() when something happens."""
        now = now if now is not None else time.monotonic()

        # An agent already working or talking does not start again. Barge-in and
        # completion are what move it out of these phases.
        #
        # But a turn that overruns gives the floor back. A 45s flight search once
        # made the agent refuse five consecutive requests as busy:processing —
        # deaf for the whole time, with no way for a human to get its attention.
        # Being asked twice is a far smaller failure than ignoring someone.
        if self.phase in (Phase.PROCESSING, Phase.RESPONDING):
            overran = (
                self._processing_since is not None
                and now - self._processing_since > MAX_PROCESSING_S
            )
            if not overran:
                return Decision(False, reason=f"busy:{self.phase.value}")
            self.phase = Phase.LISTENING
            self._processing_since = None

        if trigger.is_direct:
            # Someone asked. Bypasses cooldown and every rate limit by design
            # (spec 5.2) — refusing a direct question to respect a politeness
            # budget is worse than the interruption the budget exists to prevent.
            return Decision(
                True, frozenset({Channel.WIDGET, Channel.SPEECH}), "direct_address"
            )

        # --- proactive from here down ---
        remaining = self.cooldown_remaining(now=now)
        if remaining > 0:
            return Decision(False, reason=f"cooldown:{remaining:.1f}s")

        if confidence < PROACTIVE_WIDGET_MIN_CONFIDENCE:
            return Decision(False, reason=f"low_confidence:{confidence:.2f}")

        if self._recent(self._proactive_widgets, PROACTIVE_WIDGET_WINDOW_S, now) >= PROACTIVE_WIDGET_MAX:
            return Decision(False, reason="rate_limit:proactive_widget")

        channels = {Channel.WIDGET}

        # Speech is additive and independently gated: failing any speech check
        # silently downgrades to a widget rather than suppressing the trigger.
        speech_ok = (
            confidence >= PROACTIVE_SPEECH_MIN_CONFIDENCE
            and self._recent(self._proactive_speech, PROACTIVE_SPEECH_WINDOW_S, now)
            < PROACTIVE_SPEECH_MAX
        )
        if speech_ok:
            channels.add(Channel.SPEECH)

        reason = "proactive" if speech_ok else "proactive:widget_only"
        return Decision(True, frozenset(channels), reason)

    # --- the floor, at speak time -----------------------------------------

    def may_speak_now(
        self, *, held_since: float, direct: bool, now: float | None = None
    ) -> Decision:
        """Hold-and-expire (spec section 8), checked while audio waits in a buffer.

        Called repeatedly after synthesis completes. `held_since` is when the
        audio became ready, so the expiry is measured from readiness rather than
        from the trigger — a slow model should not eat the social window.
        """
        now = now if now is not None else time.monotonic()
        waited = now - held_since

        if self.floor.is_free(now=now):
            return Decision(True, frozenset({Channel.SPEECH}), "floor_free")

        # A direct answer keeps waiting: someone asked, and the answer is still
        # wanted. Only unprompted speech expires.
        if not direct and waited >= VOICE_EXPIRY_S:
            return Decision(False, reason="voice_expired")

        return Decision(False, reason="floor_busy")

    # --- mutations ---------------------------------------------------------

    def record_action(
        self,
        *,
        trigger: Trigger,
        channels: frozenset[Channel],
        now: float | None = None,
    ) -> None:
        """Spend the budgets an action actually used."""
        now = now if now is not None else time.monotonic()
        self._last_action_at = now
        if trigger.is_direct:
            return  # direct address spends no proactive budget
        if Channel.WIDGET in channels:
            self._proactive_widgets.append(now)
        if Channel.SPEECH in channels:
            self._proactive_speech.append(now)

    def begin(self, trigger: Trigger, *, now: float | None = None) -> None:
        """Enter PROCESSING. Call this the moment a trigger fires.

        Without it the phase guard in evaluate() is decorative. A live call fired
        three direct_address triggers in six seconds — one question plus two
        added constraints — because nothing ever left LISTENING, and the agent
        would have answered the same request three times. Holding PROCESSING is
        what makes a follow-up constraint join the turn already underway instead
        of starting a new one.
        """
        self.phase = Phase.PROCESSING
        self._processing_since = now if now is not None else time.monotonic()

    def complete(self, *, now: float | None = None) -> None:
        """The turn is done — widget rendered, speech finished or dropped."""
        self.phase = Phase.LISTENING
        self._processing_since = None

    def barge_in(self) -> None:
        """A human started talking while the agent was. Spec section 5.2: cancel
        and return to LISTENING. Deliberately does NOT start a cooldown — being
        interrupted is not the agent having had its turn."""
        self.phase = Phase.LISTENING

    def snapshot(self, *, now: float | None = None) -> dict:
        now = now if now is not None else time.monotonic()
        return {
            "phase": self.phase.value,
            "cooldown_s": round(self.cooldown_remaining(now=now), 1),
            "proactive_widgets": self._recent(self._proactive_widgets, PROACTIVE_WIDGET_WINDOW_S, now),
            "proactive_speech": self._recent(self._proactive_speech, PROACTIVE_SPEECH_WINDOW_S, now),
            "floor_busy": self.floor.busy,
            "overlapping": self.floor.overlapping,
        }
