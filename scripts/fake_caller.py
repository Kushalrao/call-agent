#!/usr/bin/env python
"""Join a LiveKit room as a synthetic participant and play a WAV as its mic.

Two of these plus the agent worker exercise the whole ears pipeline — per-track
STT, speaker attribution, ordering, overlap — with no phones and no humans
(spec section 11.3).

    .venv/bin/python scripts/fake_caller.py --room test-1 --identity rohan \\
        --name Rohan --wav tests/fixtures/audio/rohan.wav

Add --delay to stagger two callers so their speech overlaps the way real
conversation does.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import json
import time
import wave
from datetime import timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livekit import api, rtc  # noqa: E402

from control_plane.config import get_settings  # noqa: E402

FRAME_MS = 10  # LiveKit wants small frames; 10ms matches the spec's pipeline


def mint_token(room: str, identity: str, name: str) -> str:
    s = get_settings()
    if not s.livekit_configured:
        raise SystemExit("LIVEKIT_* not configured in .env")
    return (
        api.AccessToken(s.livekit_api_key, s.livekit_api_secret)
        .with_identity(identity)
        .with_name(name)
        .with_ttl(timedelta(hours=2))
        .with_grants(
            api.VideoGrants(
                room_join=True, room=room, can_publish=True,
                can_subscribe=True, can_publish_data=False,
            )
        )
        .to_jwt()
    )


async def play_wav(source: rtc.AudioSource, path: Path, sample_rate: int) -> None:
    with wave.open(str(path), "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise SystemExit(f"{path}: need mono 16-bit PCM WAV")
        if wf.getframerate() != sample_rate:
            raise SystemExit(
                f"{path}: {wf.getframerate()}Hz but publishing at {sample_rate}Hz"
            )

        samples_per_frame = sample_rate * FRAME_MS // 1000
        total = 0
        # If publishing runs slower than realtime, Deepgram's stream time falls
        # behind the wall clock and every latency measured downstream is inflated
        # by the drift. So the drift is reported, not assumed to be zero.
        started = time.monotonic()
        while True:
            data = wf.readframes(samples_per_frame)
            if not data:
                break
            # Pad the final short frame; LiveKit expects a fixed frame size.
            if len(data) < samples_per_frame * 2:
                data = data + b"\x00" * (samples_per_frame * 2 - len(data))
            frame = rtc.AudioFrame(
                data=data,
                sample_rate=sample_rate,
                num_channels=1,
                samples_per_channel=samples_per_frame,
            )
            await source.capture_frame(frame)
            total += 1
        audio_s = total * FRAME_MS / 1000
        wall_s = time.monotonic() - started
        drift = wall_s - audio_s
        warn = "  <-- SLOWER THAN REALTIME" if drift > 0.5 else ""
        print(f"[{path.name}] published {total} frames: {audio_s:.1f}s audio in "
              f"{wall_s:.1f}s wall (drift {drift:+.2f}s){warn}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", required=True)
    ap.add_argument("--identity", required=True)
    ap.add_argument("--name", default=None)
    ap.add_argument("--wav", required=True, type=Path)
    ap.add_argument("--sample-rate", type=int, default=16000)
    ap.add_argument("--delay", type=float, default=0.0, help="seconds before speaking")
    ap.add_argument("--linger", type=float, default=6.0, help="stay connected after audio ends")
    ap.add_argument("--record-agent", type=Path, default=None,
                    help="write the agent's audio track to this WAV")
    args = ap.parse_args()

    name = args.name or args.identity
    settings = get_settings()
    token = mint_token(args.room, args.identity, name)

    room = rtc.Room()

    # Receive the agent's widgets, so the agent -> client path can be verified
    # without a phone. This is the same topic and envelope CallCenter.swift
    # consumes, so a decode failure here is a decode failure there.
    received: list[dict] = []

    @room.on("data_received")
    def _on_data(packet: rtc.DataPacket) -> None:
        if packet.topic != "widget":
            return
        # Mirror the client's own gate (CallCenter.swift): it drops widgets whose
        # publisher lacks `"kind":"agent"` in its participant metadata. Checking
        # it here too is the difference between a harness that passes and a phone
        # that shows nothing.
        meta = getattr(packet.participant, "metadata", None) or ""
        if '"kind":"agent"' not in meta:
            print(f"[{args.identity}] WIDGET WOULD BE DROPPED BY THE APP: "
                  f"publisher metadata={meta!r}")
            return
        try:
            widget = json.loads(packet.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f"[{args.identity}] WIDGET UNDECODABLE: {exc}")
            return
        received.append(widget)
        payload = widget.get("payload", {})
        summary = payload.get("message") or payload.get("state") or ""
        print(f"[{args.identity}] << widget v{widget.get('v')} "
              f"{widget.get('type')} ttl={widget.get('ttl_s')}s  {summary}")

    # Record whatever the agent publishes. This is the only way to prove the
    # agent actually spoke into the call, rather than that TTS returned bytes.
    recorder: dict[str, Any] = {"task": None, "frames": 0}

    @room.on("track_subscribed")
    def _on_track(track, publication, participant):
        if args.record_agent is None:
            return
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        if "agent" not in str(participant.identity).lower():
            return
        print(f"[{args.identity}] recording agent audio from {participant.identity}")
        recorder["task"] = asyncio.create_task(_record(track))

    async def _record(track) -> None:
        stream = rtc.AudioStream.from_track(track=track, sample_rate=24000,
                                           num_channels=1)
        args.record_agent.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(args.record_agent), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            async for event in stream:
                wf.writeframes(bytes(event.frame.data))
                recorder["frames"] += 1

    await room.connect(settings.livekit_url, token)
    print(f"[{args.identity}] connected to {args.room}")

    source = rtc.AudioSource(args.sample_rate, 1)
    track = rtc.LocalAudioTrack.create_audio_track("mic", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    print(f"[{args.identity}] published mic")

    if args.delay:
        await asyncio.sleep(args.delay)

    try:
        await play_wav(source, args.wav, args.sample_rate)
        # Stay in the room so the agent's STT can finalize the last utterance —
        # leaving immediately truncates it.
        await asyncio.sleep(args.linger)
    finally:
        with contextlib.suppress(Exception):
            await room.disconnect()
        note = ""
        if args.record_agent is not None:
            secs = recorder["frames"] * 0.01
            note = (f", agent audio {secs:.1f}s -> {args.record_agent.name}"
                    if recorder["frames"] else ", NO AGENT AUDIO")
        print(f"[{args.identity}] left  ({len(received)} widgets received{note})")


if __name__ == "__main__":
    asyncio.run(main())
