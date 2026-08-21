"""The agent's mouth — TTS published into the call (spec section 8).

The agent speaks by publishing its own audio track into the LiveKit room, so both
humans hear it as part of the call. It does not send text to a phone to be spoken
there: a voice only one party could hear would be disorienting for both, and
would put the agent outside the conversation rather than in it.

Four things here are not obvious and each was a deliberate choice:

**One track, published at join, silent when idle.** Publishing at speak time costs
a renegotiation round-trip on top of an already ~600ms first byte. So the track
exists from the moment the agent joins and simply carries nothing until it does.

**An earcon before the words.** ~150ms of tone, generated locally. Attention
orients before content starts, so listeners do not miss the opening words and
mentally replay them — and it buys ~150ms of synthesis buffer, which covers a
useful slice of first-byte latency at zero cost.

**Barge-in must flush the queue, not just stop pushing.** `rtc.AudioSource` buffers
~1s ahead by default — measured, via the fake caller reporting −1.00s drift. A
cancel that only breaks the push loop leaves a second of agent audio still
playing over the human who interrupted, which is the exact rudeness barge-in
exists to prevent. Hence `clear_queue()`, and a deliberately small queue.

**Speech is cheap to abandon and expensive to get wrong.** Every path here
degrades to silence rather than raising: the humans are talking to each other and
the agent is an accessory to that (spec section 10).
"""

from __future__ import annotations

import asyncio
import math
import struct
import time
from dataclasses import dataclass, field
from typing import Any

from livekit import rtc

from control_plane.config import get_settings
from control_plane.logging_setup import Events, log_event

SAMPLE_RATE = 24000  # ElevenLabs pcm_24000; no resampling on our side
FRAME_MS = 10
# Small on purpose. The default 1000ms buffer is what makes barge-in feel laggy;
# 200ms is still ample against a 10ms frame cadence.
QUEUE_MS = 200

EARCON_MS = 150
EARCON_HZ = (660, 880)  # a gentle rising pair: "something is about to speak"
EARCON_GAIN = 0.18      # under the voice, never startling


def earcon_frames(sample_rate: int = SAMPLE_RATE) -> list[rtc.AudioFrame]:
    """A short rising two-tone, generated rather than shipped as an asset.

    Raised-cosine envelope on each note: a bare sine switched on and off clicks,
    and a click is exactly the wrong way to ask for someone's attention.
    """
    frames: list[rtc.AudioFrame] = []
    per_note_ms = EARCON_MS // len(EARCON_HZ)
    samples_per_frame = sample_rate * FRAME_MS // 1000

    pcm = bytearray()
    for hz in EARCON_HZ:
        total = sample_rate * per_note_ms // 1000
        for n in range(total):
            envelope = 0.5 * (1 - math.cos(2 * math.pi * n / max(1, total - 1)))
            value = math.sin(2 * math.pi * hz * n / sample_rate) * envelope * EARCON_GAIN
            pcm += struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767))

    for i in range(0, len(pcm), samples_per_frame * 2):
        chunk = bytes(pcm[i : i + samples_per_frame * 2])
        if len(chunk) < samples_per_frame * 2:
            chunk += b"\x00" * (samples_per_frame * 2 - len(chunk))
        frames.append(rtc.AudioFrame(
            data=chunk, sample_rate=sample_rate, num_channels=1,
            samples_per_channel=samples_per_frame,
        ))
    return frames


@dataclass
class Voice:
    """The agent's single audio track and everything that plays through it."""

    call_id: str
    room: Any = None
    source: rtc.AudioSource | None = None
    track: rtc.LocalAudioTrack | None = None
    _tts: Any = None
    _cached: dict[str, list[rtc.AudioFrame]] = field(default_factory=dict)
    _speaking: bool = False
    _cancel: asyncio.Event = field(default_factory=asyncio.Event)
    # Serialises the keepalive against real speech so silence is never
    # interleaved into a sentence.
    _floor: asyncio.Lock = field(default_factory=asyncio.Lock)
    _keepalive: asyncio.Task | None = None
    spoken_chars: int = 0

    # --- setup -------------------------------------------------------------

    async def publish(self, room: Any) -> bool:
        """Publish the (silent) track at join. Never raises."""
        self.room = room
        try:
            self.source = rtc.AudioSource(SAMPLE_RATE, 1, queue_size_ms=QUEUE_MS)
            self.track = rtc.LocalAudioTrack.create_audio_track("agent-voice", self.source)
            await room.local_participant.publish_track(
                self.track,
                rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
            )
        except Exception as exc:  # noqa: BLE001
            log_event(Events.ERROR_LIVEKIT, level="error", call_id=self.call_id,
                      op="publish_voice_track", error=str(exc))
            self.source = None
            return False
        # Keep the track producing RTP even when the agent has nothing to say.
        #
        # This is the fix for "the agent never speaks". Server-side everything was
        # correct — a Python subscriber recorded 64s containing the agent's actual
        # speech at peak 27844 — but on iOS nothing was ever heard. A track that
        # emits no packets at all for the first minute of a call looks inactive to
        # the SFU and to a subscriber, so no playback path is established; by the
        # time real audio arrives there is nothing listening for it. Continuous
        # silence keeps the stream established, which costs a few kbit/s of zeros.
        self._keepalive = asyncio.create_task(self._emit_silence())

        log_event("voice.track_published", call_id=self.call_id,
                  sample_rate=SAMPLE_RATE, queue_ms=QUEUE_MS)
        return True

    async def _emit_silence(self) -> None:
        """Push silent frames whenever the agent is not talking.

        `capture_frame` paces itself against the source's queue, so this runs at
        real time on its own without a sleep loop to tune.
        """
        samples = SAMPLE_RATE * FRAME_MS // 1000
        blank = b"\x00" * (samples * 2)
        while self.source is not None:
            try:
                if self._speaking:
                    await asyncio.sleep(0.02)
                    continue
                async with self._floor:
                    if self._speaking:
                        continue
                    await self.source.capture_frame(rtc.AudioFrame(
                        data=blank, sample_rate=SAMPLE_RATE, num_channels=1,
                        samples_per_channel=samples,
                    ))
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                # Never let the keepalive take the call down.
                await asyncio.sleep(0.2)

    def _make_tts(self) -> Any:
        if self._tts is not None:
            return self._tts
        from livekit.plugins import elevenlabs

        s = get_settings()
        if not s.elevenlabs_api_key:
            return None
        self._tts = elevenlabs.TTS(
            api_key=s.elevenlabs_api_key,
            voice_id=s.elevenlabs_voice_id,
            model=s.elevenlabs_model,
            # Match the track so nothing has to be resampled mid-sentence.
            encoding="pcm_24000",
        )
        return self._tts

    @property
    def available(self) -> bool:
        return self.source is not None and bool(get_settings().elevenlabs_api_key)

    @property
    def speaking(self) -> bool:
        return self._speaking

    # --- barge-in ----------------------------------------------------------

    def barge_in(self, *, reason: str = "human_speech") -> None:
        """Stop talking now. Safe to call when not speaking."""
        if not self._speaking:
            return
        self._cancel.set()
        if self.source is not None:
            # The queue is the whole point: without this, ~QUEUE_MS of already
            # buffered speech keeps playing over the person who interrupted.
            try:
                self.source.clear_queue()
            except Exception:  # noqa: BLE001
                pass
        log_event(Events.TTS_CANCELLED, call_id=self.call_id, reason=reason)

    # --- speaking ----------------------------------------------------------

    async def prepare(self, *phrases: str) -> None:
        """Synthesize fixed phrases once, at join, and keep the PCM in memory.

        This is the whole latency story for the agent's first sound. Synthesizing
        on demand costs a network round trip to ElevenLabs — measured at 971ms
        warm and 2265ms cold from here — on top of the ~1700ms wait for a final
        transcript. Nearly three seconds before a human hears anything after
        asking a direct question, which does not read as an assistant; it reads as
        a broken one.

        A phrase that never changes does not need to be synthesized twice. Cached,
        the same sound plays with no network at all, which moves the first
        response to roughly the interim-transcript latency alone (~500ms) — fast
        enough to feel like an answer rather than a delay.

        Costs a handful of characters against the ElevenLabs quota, once per call.
        """
        tts = self._make_tts()
        if tts is None or self.source is None:
            return
        for phrase in phrases:
            if phrase in self._cached:
                continue
            frames: list[rtc.AudioFrame] = []
            t = time.perf_counter()
            try:
                stream = tts.synthesize(phrase)
                async for chunk in stream:
                    frames.append(chunk.frame)
                await stream.aclose()
            except Exception as exc:  # noqa: BLE001
                log_event("voice.prepare_failed", level="warn", call_id=self.call_id,
                          phrase=phrase, error=str(exc))
                continue
            self._cached[phrase] = frames
            self.spoken_chars += len(phrase)
            log_event("voice.prepared", call_id=self.call_id, phrase=phrase,
                      frames=len(frames),
                      synth_ms=round((time.perf_counter() - t) * 1000, 1))

    async def say_cached(self, phrase: str, *, earcon: bool = True) -> bool:
        """Play a prepared phrase. No network, so effectively instant.

        Falls back to normal synthesis if it was never prepared — better a slow
        answer than none.
        """
        frames = self._cached.get(phrase)
        if frames is None:
            log_event("voice.cache_miss", level="warn", call_id=self.call_id,
                      phrase=phrase)
            return await self.say(phrase, earcon=earcon)
        if not self.available or self._speaking:
            return False

        self._speaking = True
        self._cancel.clear()
        started = time.perf_counter()
        log_event(Events.TTS_START, call_id=self.call_id, cached=True,
                  chars=len(phrase), text=phrase)
        try:
            async with self._floor:
                if earcon:
                    for frame in earcon_frames():
                        if self._cancel.is_set():
                            break
                        await self.source.capture_frame(frame)
                for frame in frames:
                    if self._cancel.is_set():
                        break
                    await self.source.capture_frame(frame)
        except Exception as exc:  # noqa: BLE001
            log_event(Events.ERROR_INTERNAL, level="error", call_id=self.call_id,
                      stage="say_cached", error=str(exc))
            return False
        finally:
            self._speaking = False
        log_event("voice.finished", call_id=self.call_id, cached=True,
                  completed=not self._cancel.is_set(),
                  first_byte_ms=0.0,
                  total_ms=round((time.perf_counter() - started) * 1000, 1),
                  spoken_chars_total=self.spoken_chars)
        return not self._cancel.is_set()

    async def say(self, text: str, *, earcon: bool = True) -> bool:
        """Synthesize and play. Returns whether it finished uninterrupted."""
        if not text.strip():
            return False
        if not self.available:
            log_event("voice.unavailable", level="warn", call_id=self.call_id,
                      reason="no_track" if self.source is None else "no_api_key")
            return False
        if self._speaking:
            log_event("voice.busy", level="warn", call_id=self.call_id)
            return False

        tts = self._make_tts()
        if tts is None:
            return False

        self._speaking = True
        self._cancel.clear()
        started = time.perf_counter()
        first_byte_ms: float | None = None
        completed = False

        log_event(Events.TTS_START, call_id=self.call_id, chars=len(text), text=text)
        try:
            await self._floor.acquire()
            if earcon:
                # Plays while synthesis is still in flight, so it is free latency.
                for frame in earcon_frames():
                    if self._cancel.is_set():
                        break
                    await self.source.capture_frame(frame)

            stream = tts.synthesize(text)
            async for chunk in stream:
                if self._cancel.is_set():
                    break
                if first_byte_ms is None:
                    first_byte_ms = round((time.perf_counter() - started) * 1000, 1)
                    log_event(Events.TTS_FIRST_BYTE, call_id=self.call_id,
                              latency_ms=first_byte_ms)
                await self.source.capture_frame(chunk.frame)
            else:
                completed = True
            await stream.aclose()
        except Exception as exc:  # noqa: BLE001
            log_event(Events.ERROR_INTERNAL, level="error", call_id=self.call_id,
                      stage="tts", error=str(exc))
        finally:
            if self._floor.locked():
                self._floor.release()
            self._speaking = False
            self.spoken_chars += len(text)
            log_event(
                "voice.finished", call_id=self.call_id,
                completed=completed,
                first_byte_ms=first_byte_ms,
                total_ms=round((time.perf_counter() - started) * 1000, 1),
                # The free ElevenLabs tier is ~10k characters, so this is a real
                # budget rather than a curiosity.
                spoken_chars_total=self.spoken_chars,
            )
        return completed

    def summary(self) -> dict[str, Any]:
        return {"spoken_chars": self.spoken_chars}
