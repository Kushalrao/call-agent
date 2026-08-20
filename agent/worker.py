"""The in-call agent worker — ears only (spec section 4).

Deliberately NOT using `AgentSession`. That class assumes one human, auto-detects
turns, and replies after each user turn. This agent is *ambient*: it listens to
two humans and acts only when our own decision layer says so. So we keep the
framework's dispatch and lifecycle, take `ctx.room` as a plain rtc Room, and do
the audio plumbing ourselves.

    remote audio track (one per human)
      -> rtc.AudioStream frames
      -> one Deepgram streaming session per track
           interim  -> fast path hook (wake word, phase 3)
           final    -> TranscriptAggregator
      -> speaker-attributed, time-ordered transcript

No diarization anywhere: each STT session is bound to one participant's track, so
speaker identity is a property of the plumbing rather than a guess.

Run:
    .venv/bin/python -m agent.worker dev
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from livekit import rtc
from livekit.agents import JobContext, WorkerOptions, cli
from livekit.agents import stt as agent_stt
from livekit.plugins import deepgram

from control_plane.config import get_settings
from control_plane.logging_setup import Events, log_event
from .budget import Budget
from .classifier import Classifier
from .policy import Phase, Policy, Trigger
from .search import destination_from, origin_from, run_search, to_sentence
from .transcript import StreamClock, TranscriptAggregator
from .vocabulary import KEYTERMS
from .wake import FastPath
from .voice import Voice
from .widgets import WidgetPublisher

# Spec section 4.1. `multi` is the point: our users code-switch between Hindi and
# English mid-sentence, and a monolingual model silently drops half of it.
STT_MODEL = os.environ.get("STT_MODEL", "nova-3")
STT_LANGUAGE = os.environ.get("STT_LANGUAGE", "multi")
ENDPOINTING_MS = int(os.environ.get("STT_ENDPOINTING_MS", "300"))

# Ring buffer for STT reconnects (spec section 4.1). Fifteen seconds of audio at
# 16kHz mono PCM16 is ~480KB — cheap insurance against losing the middle of a
# sentence when a socket drops.
RING_BUFFER_SECONDS = 15.0
MAX_RECONNECTS_IN_WINDOW = 3
RECONNECT_WINDOW_S = 30.0


def _peak_amplitude(frame: rtc.AudioFrame) -> int:
    """Loudest sample in one frame, as absolute int16.

    Cheap on purpose — this runs on every frame of every speaker, so it reads the
    raw buffer with array() rather than pulling in numpy.
    """
    import array

    try:
        samples = array.array("h")
        samples.frombytes(bytes(frame.data))
    except (ValueError, TypeError):
        return 0
    if not samples:
        return 0
    return max(max(samples), -min(samples))


def make_stt() -> deepgram.STT:
    """A fresh streaming session. One per track, and a new one per reconnect —
    a Deepgram stream cannot be reopened once its socket has gone."""
    s = get_settings()
    if not s.stt_configured:
        raise RuntimeError("DEEPGRAM_API_KEY is not set; the agent cannot hear")
    return deepgram.STT(
        api_key=s.deepgram_api_key,
        model=STT_MODEL,
        language=STT_LANGUAGE,
        interim_results=True,
        smart_format=True,
        punctuate=True,
        endpointing_ms=ENDPOINTING_MS,
        sample_rate=16000,
        # Place names are the highest-stakes words in the whole transcript.
        keyterms=KEYTERMS,
    )


@dataclass
class TrackTranscriber:
    """One participant's audio -> one STT session -> the shared transcript."""

    participant: rtc.RemoteParticipant
    aggregator: TranscriptAggregator
    call_id: str
    on_interim: Callable[[str, str], None] | None = None
    on_final: Callable[[Any], None] | None = None
    on_speech_start: Callable[[str], None] | None = None
    on_speech_end: Callable[[str], None] | None = None

    clock: StreamClock = field(default_factory=StreamClock)
    degraded: bool = False
    _segment_open: bool = False
    _reconnects: deque[float] = field(default_factory=deque)
    _ring: deque[tuple[rtc.AudioFrame, float]] = field(default_factory=deque)
    _ring_seconds: float = 0.0

    @property
    def speaker_id(self) -> str:
        ident = self.participant.identity
        return ident.stringValue if hasattr(ident, "stringValue") else str(ident)

    @property
    def speaker_name(self) -> str:
        return self.participant.name or self.speaker_id

    # --- ring buffer -------------------------------------------------------

    def _remember(self, frame: rtc.AudioFrame) -> None:
        duration = frame.samples_per_channel / max(frame.sample_rate, 1)
        self._ring.append((frame, duration))
        self._ring_seconds += duration
        while self._ring_seconds > RING_BUFFER_SECONDS and self._ring:
            _, dropped = self._ring.popleft()
            self._ring_seconds -= dropped

    def _replay_into(self, stream: Any) -> float:
        replayed = 0.0
        for frame, duration in list(self._ring):
            stream.push_frame(frame)
            replayed += duration
        return replayed

    def _too_many_reconnects(self) -> bool:
        now = time.monotonic()
        while self._reconnects and now - self._reconnects[0] > RECONNECT_WINDOW_S:
            self._reconnects.popleft()
        return len(self._reconnects) >= MAX_RECONNECTS_IN_WINDOW

    # --- run ---------------------------------------------------------------

    async def run(self, track: rtc.Track) -> None:
        """Pump one track into Deepgram, reconnecting on failure.

        The call must survive anything that happens in here: a permanently
        broken STT socket means the agent goes deaf for one speaker, and that is
        all it means (spec section 10).
        """
        audio = rtc.AudioStream.from_track(track=track, sample_rate=16000, num_channels=1)
        attempt = 0

        while True:
            stream = make_stt().stream()

            # On a reconnect the ring buffer is pushed immediately, so Deepgram's
            # offset 0 is the *oldest replayed* frame — the epoch is backdated by
            # the replayed duration. On a first attempt there is nothing to
            # replay and the epoch is set on the first real frame below, because
            # a track can be subscribed well before it delivers audio.
            replayed = 0.0
            if attempt:
                replayed = self._replay_into(stream)
                self.clock.restart(replayed_seconds=replayed)

            log_event(
                Events.STT_SESSION_OPEN,
                call_id=self.call_id,
                speaker_id=self.speaker_id,
                attempt=attempt,
                replayed_s=round(replayed, 2),
                model=STT_MODEL,
                language=STT_LANGUAGE,
            )

            reader = asyncio.create_task(self._read_events(stream))
            try:
                frames = 0
                # Peak amplitude since the last report. A track can deliver
                # frames forever while containing pure silence — the mic is
                # published but capturing nothing — and that is indistinguishable
                # from "STT is broken" unless the level is measured. This is the
                # difference between debugging iOS and debugging Deepgram.
                peak = 0
                # First report at 2s, not 10s. Every real call so far has been
                # under fifteen seconds, so a ten-second cadence produced no
                # level reading at all — the one number needed to tell a dead mic
                # from a broken STT. Cheap diagnostics that only arrive on long
                # calls are not diagnostics.
                next_report = time.monotonic() + 2.0
                reports = 0
                async for event in audio:
                    frame = event.frame
                    if not self.clock.started:
                        self.clock.start()
                        # First frame is the fact worth knowing: a call that
                        # produced no transcript is either "no audio arrived" or
                        # "audio arrived and STT returned nothing", and those have
                        # completely different causes. Without this the logs
                        # cannot tell them apart.
                        log_event("stt.first_frame", call_id=self.call_id,
                                  speaker_id=self.speaker_id,
                                  sample_rate=frame.sample_rate)
                    self._remember(frame)
                    stream.push_frame(frame)
                    frames += 1
                    peak = max(peak, _peak_amplitude(frame))
                    if time.monotonic() >= next_report:
                        reports += 1
                        next_report = time.monotonic() + (2.0 if reports < 3 else 10.0)
                        log_event(
                            "stt.audio_flowing", call_id=self.call_id,
                            speaker_id=self.speaker_id, frames=frames,
                            seconds=round(frames * 0.01, 1),
                            peak=peak,
                            # int16 full scale is 32767. Under ~100 is silence
                            # or a dead mic, not quiet speech.
                            silent=peak < 100,
                        )
                        peak = 0
                if frames == 0:
                    log_event("stt.no_audio", level="error", call_id=self.call_id,
                              speaker_id=self.speaker_id,
                              detail="track subscribed but delivered zero frames — "
                                     "the participant's mic never reached us")
                # Track ended cleanly: the participant left or unpublished.
                stream.end_input()
                await reader
                return
            except Exception as exc:  # noqa: BLE001
                reader.cancel()
                self._reconnects.append(time.monotonic())
                log_event(
                    Events.STT_RECONNECT,
                    level="warn",
                    call_id=self.call_id,
                    speaker_id=self.speaker_id,
                    error=str(exc),
                    attempt=attempt,
                )
                if self._too_many_reconnects():
                    self.degraded = True
                    log_event(
                        Events.STT_DEGRADED,
                        level="error",
                        call_id=self.call_id,
                        speaker_id=self.speaker_id,
                        detail=(
                            f"{MAX_RECONNECTS_IN_WINDOW} failures in "
                            f"{RECONNECT_WINDOW_S:.0f}s — this speaker is no longer "
                            "being transcribed; the call continues"
                        ),
                    )
                    return
                attempt += 1
                await asyncio.sleep(min(2**attempt, 8))
            finally:
                await stream.aclose()

    async def _read_events(self, stream: Any) -> None:
        async for event in stream:
            if event.type == agent_stt.SpeechEventType.START_OF_SPEECH:
                # Feeds Policy.floor, which is what hold-and-expire consults.
                # Without this the floor always looks free and the agent talks
                # straight over people.
                if self.on_speech_start:
                    self.on_speech_start(self.speaker_id)
                continue

            if event.type == agent_stt.SpeechEventType.END_OF_SPEECH:
                if self.on_speech_end:
                    self.on_speech_end(self.speaker_id)
                continue

            if event.type == agent_stt.SpeechEventType.INTERIM_TRANSCRIPT:
                # The fast path (spec section 5.1a): wake-word matching runs on
                # interims so direct address is detected mid-sentence rather than
                # after endpointing finalizes.
                alt = event.alternatives[0] if event.alternatives else None
                if not alt or not alt.text:
                    continue
                if not self.clock.started:
                    self.clock.start()

                # Log only the first interim of each speech segment. Interims
                # arrive several times a second, so logging all of them would
                # bury the call; the first one is the number that matters — it is
                # how quickly the agent can know someone has started speaking.
                if not self._segment_open:
                    self._segment_open = True
                    log_event(
                        Events.STT_INTERIM,
                        call_id=self.call_id,
                        speaker_id=self.speaker_id,
                        first_in_segment=True,
                        interim_lag_ms=round(
                            (time.monotonic() - self.clock.absolute(alt.end_time)) * 1000, 1
                        ),
                        text=alt.text,
                    )
                if self.on_interim:
                    self.on_interim(self.speaker_id, alt.text)

            elif event.type == agent_stt.SpeechEventType.FINAL_TRANSCRIPT:
                self._segment_open = False
                alt = event.alternatives[0] if event.alternatives else None
                if not alt or not alt.text.strip():
                    continue
                if not self.clock.started:
                    self.clock.start()

                # How long after someone stopped talking the transcript became
                # usable. This is the floor under every trigger latency budget in
                # spec section 5, so it is measured rather than assumed.
                finalize_lag_ms = None
                utterance = self.aggregator.add(
                    speaker_id=self.speaker_id,
                    speaker_name=self.speaker_name,
                    text=alt.text,
                    t_start=self.clock.absolute(alt.start_time),
                    t_end=self.clock.absolute(alt.end_time),
                    confidence=alt.confidence if alt.confidence is not None else 1.0,
                )
                finalize_lag_ms = round((time.monotonic() - utterance.t_end) * 1000, 1)
                log_event(
                    Events.AGGREGATOR_UTTERANCE,
                    call_id=self.call_id,
                    utterance_id=utterance.utterance_id,
                    speaker_id=utterance.speaker_id,
                    speaker_name=utterance.speaker_name,
                    confidence=round(utterance.confidence, 3),
                    duration_ms=int((utterance.t_end - utterance.t_start) * 1000),
                    finalize_lag_ms=finalize_lag_ms,
                    # Privacy split (spec 11.1): dropped unless LOG_TRANSCRIPTS.
                    text=utterance.text,
                )
                if self.on_final:
                    self.on_final(utterance)


class CallAgent:
    """Per-call state. One of these per room."""

    def __init__(self, call_id: str, *, classifier: Classifier | None = None) -> None:
        self.call_id = call_id
        self.aggregator = TranscriptAggregator()
        self.transcribers: dict[str, TrackTranscriber] = {}
        self.fast_path = FastPath()
        self.policy = Policy()
        self.classifier = classifier or Classifier()
        # Set once the room is connected; until then there is nowhere to publish.
        self.widgets: WidgetPublisher | None = None
        self.voice = Voice(call_id=call_id)
        # One search at a time, and never the same route twice in a call.
        self._searching = False
        self._searched: set[str] = set()
        # Speakers whose current segment matched the wake name. The fast path is
        # an independent route to acting, not just to acknowledging.
        self._wake_fired: set[str] = set()
        # Per-call spend authority. Ceilings are refusals, not warnings.
        self.budget = Budget.from_env(call_id)
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro: Any) -> None:
        """Fire-and-forget with a strong reference, so the task is not garbage
        collected mid-flight — asyncio only holds a weak one."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def on_interim(self, speaker_id: str, text: str) -> None:
        """The fast path (spec section 5.1a). Runs on every interim, locally.

        This is where the measured latency makes the design load-bearing: at a
        ~1700ms finalize lag, anything that waits for FINAL cannot answer a
        direct address in time to feel like an answer.
        """
        if self.voice.speaking:
            # Spec section 5.2: cancel within 200ms. An interim is the earliest
            # signal we have that a human has taken the floor back.
            self.voice.barge_in()
            self.policy.barge_in()

        match = self.fast_path.on_interim(speaker_id, text)
        if match is not None:
            decision = self.policy.evaluate(Trigger.DIRECT_ADDRESS)
            if decision.fire:
                self._wake_fired.add(speaker_id)
                self.policy.begin(Trigger.DIRECT_ADDRESS)
                self.policy.record_action(
                    trigger=Trigger.DIRECT_ADDRESS, channels=decision.channels
                )
            log_event(
                Events.TRIGGER_FIRED if decision.fire else Events.TRIGGER_SUPPRESSED,
                call_id=self.call_id,
                speaker_id=speaker_id,
                trigger=Trigger.DIRECT_ADDRESS.value,
                path="fast",
                matched=match.matched_text,
                reason=decision.reason,
                channels=sorted(c.value for c in decision.channels) or None,
                **self.policy.snapshot(),
            )
            if decision.fire:
                if self.widgets is not None:
                    # Acknowledge before any model work starts. At a ~1050ms
                    # classifier and a multi-second reasoning turn, being
                    # addressed and showing nothing reads as broken (5.1a).
                    self._spawn(self.widgets.status("thinking", "One moment…"))
                # Phase 3 continues here: warm the reasoning turn on the partial.
                # Released for the same reason as the semantic path — claiming a
                # turn with nothing to finish it leaves the agent stuck in
                # PROCESSING and trigger-deaf for the rest of the call.
                self.policy.complete()
            return

        # Speculative prefetch on weak evidence — cheap, discardable, and never
        # a trigger (spec section 6 makes prefetch mandatory given browser latency).
        warmed = self.fast_path.should_warm(speaker_id, text)
        if warmed:
            log_event(
                "prefetch.warmed",
                call_id=self.call_id,
                speaker_id=speaker_id,
                keywords=list(warmed),
            )

    def on_speech_start(self, speaker_id: str) -> None:
        self.policy.floor.speech_started(speaker_id)

    def on_speech_end(self, speaker_id: str) -> None:
        self.policy.floor.speech_ended(speaker_id)

    def on_final(self, utterance: Any) -> None:
        # Closes the speech segment so the next phrase can fire again.
        self.fast_path.on_final(utterance.speaker_id)

        # Either path can decide we were addressed, and neither may veto the
        # other. Both failure modes have now been seen on live calls: STT
        # mangled "Hey copilot" to "Alert." and only the classifier caught it;
        # then the classifier timed out at 1502ms and only the wake word caught
        # it. Depending on either alone drops roughly one request in three.
        wake_fired = utterance.speaker_id in self._wake_fired
        self._wake_fired.discard(utterance.speaker_id)
        if wake_fired:
            destination = destination_from(getattr(utterance, "text", "") or "")
            if destination is not None:
                self._spawn(self._search_and_speak(destination, utterance))
                return
        # The semantic path (spec section 5.1b): every finalized utterance, no
        # debouncing. Spawned rather than awaited so a slow classifier never
        # backs up the STT event loop and delays the next utterance.
        self._spawn(self._classify(utterance))

    async def _classify(self, utterance: Any) -> None:
        """Classify, then let the policy decide. Never raises."""
        try:
            window = self.aggregator.render_window()
            result = await self.classifier.classify(
                window, call_id=self.call_id, budget=self.budget
            )
            trigger = result.intent.to_trigger()
            if trigger is None:
                return

            decision = self.policy.evaluate(trigger, confidence=result.confidence)
            if decision.fire:
                self.policy.begin(trigger)
                # Spend the budget at the moment of firing, not after the turn
                # completes. Firing IS the commitment to act; the pure-query
                # split exists to protect against evaluations that never fire,
                # not against fires that later fail. A failed turn therefore
                # forfeits its slot — deliberately, because the alternative is
                # an agent that retries into the same conversation.
                self.policy.record_action(trigger=trigger, channels=decision.channels)
            log_event(
                Events.TRIGGER_FIRED if decision.fire else Events.TRIGGER_SUPPRESSED,
                call_id=self.call_id,
                utterance_id=getattr(utterance, "utterance_id", None),
                speaker_id=utterance.speaker_id,
                trigger=trigger.value,
                path="semantic",
                confidence=round(result.confidence, 3),
                classifier_ms=result.latency_ms,
                reason=decision.reason,
                channels=sorted(c.value for c in decision.channels) or None,
                **self.policy.snapshot(),
            )
            if decision.fire:
                destination = (
                    destination_from(getattr(utterance, "text", "") or "")
                    if trigger is Trigger.DIRECT_ADDRESS
                    else None
                )
                if destination is not None:
                    # _searched dedupes if the fast path already started this one.
                    await self._search_and_speak(destination, utterance)
                else:
                    if self.widgets is not None:
                        await self.widgets.status("searching", "Checking flights…")
                    # No reasoning turn yet, so the turn has to be released
                    # explicitly or the agent stays in PROCESSING for the rest of
                    # the call.
                    self.policy.complete()
        except Exception as exc:  # noqa: BLE001
            # An ambient agent that crashes must not take the call with it.
            log_event(Events.ERROR_INTERNAL, level="error", call_id=self.call_id,
                      stage="classify", error=str(exc))

    async def _search_and_speak(self, destination: str, utterance: Any) -> None:
        """Real local search, then speak the cheapest into the call.

        The turn is already claimed by the caller; this releases it.
        """
        try:
            if destination in self._searched:
                log_event("search.skipped", call_id=self.call_id,
                          destination=destination, reason="already_searched")
                return
            if self._searching:
                log_event("search.skipped", call_id=self.call_id,
                          destination=destination, reason="search_in_flight")
                return

            self._searching = True
            self._searched.add(destination)
            if self.policy.phase is not Phase.PROCESSING:
                self.policy.begin(Trigger.DIRECT_ADDRESS)
            try:
                if self.widgets is not None:
                    await self.widgets.status("searching", "Checking flights…")
                outcome = await run_search(
                    destination,
                    call_id=self.call_id,
                    origin=origin_from(getattr(utterance, "text", "") or ""),
                )
                sentence = to_sentence(outcome)

                # Someone asked, so this waits for the floor rather than expiring
                # (spec section 8). The widget already carried the acknowledgement.
                held_since = time.monotonic()
                while not self.policy.may_speak_now(
                    held_since=held_since, direct=True
                ).fire:
                    await asyncio.sleep(0.1)
                    if time.monotonic() - held_since > 15.0:
                        log_event("voice.dropped", level="warn",
                                  call_id=self.call_id, reason="floor_never_opened")
                        return
                await self.voice.say(sentence)
            finally:
                self._searching = False
                self.policy.complete()
        except Exception as exc:  # noqa: BLE001
            self._searching = False
            self.policy.complete()
            log_event(Events.ERROR_INTERNAL, level="error", call_id=self.call_id,
                      stage="search_and_speak", error=str(exc))

    def attach(self, track: rtc.Track, participant: rtc.RemoteParticipant) -> None:
        ident = str(participant.identity)
        if ident in self.transcribers:
            log_event("stt.already_attached", call_id=self.call_id, speaker_id=ident)
            return

        transcriber = TrackTranscriber(
            participant=participant,
            aggregator=self.aggregator,
            call_id=self.call_id,
            on_interim=self.on_interim,
            on_final=self.on_final,
            on_speech_start=self.on_speech_start,
            on_speech_end=self.on_speech_end,
        )
        self.transcribers[ident] = transcriber

        task = asyncio.create_task(transcriber.run(track))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        log_event(
            Events.TRACK_SUBSCRIBED,
            call_id=self.call_id,
            speaker_id=ident,
            speaker_name=participant.name or ident,
            streams=len(self.transcribers),
        )

    def summary(self) -> dict[str, Any]:
        return {
            "utterances": len(self.aggregator),
            "speakers": len(self.transcribers),
            "degraded": [s for s, t in self.transcribers.items() if t.degraded],
            **(self.widgets.summary() if self.widgets else {}),
            **self.voice.summary(),
        }


def extract_call_id(room_metadata: str | None, job_metadata: str | None) -> str:
    """Pull call_id out of whichever metadata channel carried it.

    Both sources hold the same JSON envelope, but they are populated at
    different times: room metadata is set by the control plane and may not have
    arrived in this process before connect, while job metadata comes down with
    the dispatch itself. Both are tried, and both are parsed — reading the raw
    JSON string as a call_id is how a transcript once ended up in a file named
    `{"call_id":"..."}.json`.

    The result is used to build filenames, so it is sanitized: metadata is data
    from outside this process, and a call_id of `../../x` must not be able to
    steer a log write out of the log directory.
    """
    for raw in (room_metadata, job_metadata):
        if not raw:
            continue
        candidate: str | None = None
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            candidate = raw  # a bare id, not an envelope
        else:
            if isinstance(parsed, dict):
                candidate = parsed.get("call_id")
            elif isinstance(parsed, str):
                candidate = parsed
        safe = sanitize_call_id(candidate)
        if safe:
            return safe
    return "unknown"


def sanitize_call_id(value: Any) -> str | None:
    """Keep only what is safe in a path segment. Returns None if nothing is left."""
    if not isinstance(value, str):
        return None
    cleaned = "".join(c for c in value.strip() if c.isalnum() or c in "-_")
    return cleaned[:128] or None


async def entrypoint(ctx: JobContext) -> None:
    # call_id rides in room metadata so every log line in every service can be
    # correlated to one conversation (spec section 11.1).
    call_id = extract_call_id(
        getattr(ctx.room, "metadata", None), getattr(ctx.job, "metadata", None)
    )

    # Reuse the prewarmed classifier and its warm connection pool.
    agent = CallAgent(call_id, classifier=ctx.proc.userdata.get("classifier"))
    log_event(Events.AGENT_JOINED, call_id=call_id, room=ctx.room.name)

    @ctx.room.on("track_subscribed")
    def _on_track(
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            agent.attach(track, participant)

    @ctx.room.on("participant_disconnected")
    def _on_left(participant: rtc.RemoteParticipant) -> None:
        log_event(
            "call.participant_left",
            call_id=call_id,
            speaker_id=str(participant.identity),
            **agent.summary(),
        )

    async def _on_shutdown() -> None:
        # Persist the transcript (spec section 9 as amended: transcripts ARE
        # kept, 30-day TTL). Raw audio never is.
        records = agent.aggregator.to_records()
        log_event(
            Events.AGENT_REMOVED,
            call_id=call_id,
            **agent.summary(),
            transcript_utterances=len(records),
        )
        # Every call reports its own cost, so spend is visible per call rather
        # than as a surprise at the end of the month.
        log_event("llm.call_total", call_id=call_id, **agent.budget.summary())
        out_dir = os.environ.get("TRANSCRIPT_DIR", "logs")
        try:
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, f"{call_id}.transcript.json"), "w") as fh:
                json.dump(
                    {"call_id": call_id, "utterances": records},
                    fh,
                    indent=1,
                )
        except OSError as exc:
            log_event(Events.ERROR_INTERNAL, level="error", call_id=call_id, error=str(exc))

    ctx.add_shutdown_callback(_on_shutdown)

    # Subscribe to everything; we filter to audio above. The agent publishes no
    # audio track yet — that arrives with the voice path.
    await ctx.connect()

    # The client verifies that widgets came from the agent by checking for
    # `"kind":"agent"` in the publisher's participant metadata
    # (CallCenter.swift). `mint_agent_token` sets that, but this worker never
    # uses that token — the Agents framework mints its own on connect — so
    # without this the agent joins with no metadata and every widget is dropped
    # client-side as "from non-agent participant". Nothing logs an error on
    # either side; the toast simply never appears.
    try:
        await ctx.room.local_participant.set_metadata('{"kind":"agent"}')
        log_event("agent.metadata_set", call_id=call_id, kind="agent")
    except Exception as exc:  # noqa: BLE001
        log_event(Events.ERROR_LIVEKIT, level="error", call_id=call_id,
                  op="set_agent_metadata", error=str(exc))

    agent.widgets = WidgetPublisher(ctx.room, call_id)
    # Published now, silent until needed: publishing at speak time would add a
    # renegotiation round-trip on top of an already ~600ms first byte.
    await agent.voice.publish(ctx.room)

    if call_id == "unknown":
        # Room metadata only reaches this process once connected.
        recovered = extract_call_id(getattr(ctx.room, "metadata", None), None)
        if recovered != "unknown":
            log_event("agent.call_id_recovered", call_id=recovered, room=ctx.room.name)
            call_id = agent.call_id = recovered
            agent.widgets.call_id = recovered
            for t in agent.transcribers.values():
                t.call_id = recovered

    # Attach to tracks already published before we joined.
    for participant in ctx.room.remote_participants.values():
        for publication in participant.track_publications.values():
            if publication.track and publication.kind == rtc.TrackKind.KIND_AUDIO:
                agent.attach(publication.track, participant)


def prewarm(proc: Any) -> None:
    """Runs once per worker process, before it takes a job.

    Measured: the first classifier call costs ~2041ms against a 1.5s timeout,
    purely connection and schema setup. Without this the first utterance of the
    first call of the day is guaranteed to be missed.
    """
    classifier = Classifier()
    proc.userdata["classifier"] = classifier
    if classifier.available:
        asyncio.get_event_loop().run_until_complete(classifier.prewarm())


if __name__ == "__main__":
    settings = get_settings()
    # Fail here, not on the first utterance of a real call.
    if not settings.stt_configured:
        raise SystemExit("DEEPGRAM_API_KEY is not set in .env — the agent cannot hear")
    if not settings.livekit_configured:
        raise SystemExit("LIVEKIT_* is not set in .env")
    # Credentials come from Settings, not os.environ: config.py is the only
    # thing that reads .env, so there is one place to look when a key is wrong.
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name=settings.agent_name,
            ws_url=settings.livekit_url,
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
        )
    )
