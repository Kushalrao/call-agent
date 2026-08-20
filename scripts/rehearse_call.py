#!/usr/bin/env python
"""Rehearse a whole call with no phones and no humans (spec section 11.3).

Creates a room the way the control plane does (metadata carries call_id),
explicitly dispatches the agent worker into it, then plays two fixture WAVs as
two participants' mics with a stagger so their speech overlaps. Ends the call,
which triggers the worker's shutdown hook and persists the transcript.

The agent worker must already be running:

    LOG_SERVICE=agent-worker .venv/bin/python -m agent.worker dev

Then:

    .venv/bin/python scripts/rehearse_call.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from control_plane import livekit_gateway as gw  # noqa: E402
from control_plane.config import get_settings  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "audio"
VENV_PY = ROOT / ".venv" / "bin" / "python"

# Rohan opens; Kushal answers 4s in, so his first line lands over the tail of
# Rohan's — overlap is the normal case, not an edge case.
# Two scenarios. `planning` is ambient trip talk with nobody addressing the
# agent; `direct` addresses it by name, which is what exercises the fast path.
SCENARIOS = {
    "planning": [
        {"identity": "u-rohan", "name": "Rohan", "wav": "rohan.wav", "delay": 0.0},
        {"identity": "u-kushal", "name": "Kushal", "wav": "kushal.wav", "delay": 4.0},
    ],
    # The first end-to-end loop: one sentence, real search, spoken answer.
    "flights": [
        {"identity": "u-rohan", "name": "Rohan", "wav": "rohan_flights.wav", "delay": 0.0},
    ],
    "direct": [
        {"identity": "u-rohan", "name": "Rohan", "wav": "rohan_direct.wav", "delay": 0.0},
        {"identity": "u-kushal", "name": "Kushal", "wav": "kushal_reply.wav", "delay": 7.0},
    ],
}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--call-id", default=None)
    ap.add_argument("--linger", type=float, default=6.0,
                    help="seconds each caller stays after its audio ends")
    ap.add_argument("--scenario", default="planning", choices=sorted(SCENARIOS),
                    help="planning = ambient trip talk; direct = addresses the agent by name")
    ap.add_argument("--keep-room", action="store_true",
                    help="do not delete the room at the end")
    args = ap.parse_args()

    settings = get_settings()
    call_id = args.call_id or f"rehearse-{uuid.uuid4().hex[:8]}"
    room_name = f"call-{call_id}"

    print(f"call_id  = {call_id}")
    print(f"room     = {room_name}")
    print(f"scenario = {args.scenario}")
    print(f"livekit  = {settings.livekit_url}\n")

    await gw.create_room(room_name=room_name, call_id=call_id)
    await gw.prepare_and_dispatch(room_name=room_name, call_id=call_id)
    print("room created + agent dispatched; waiting 3s for the worker to join\n")
    await asyncio.sleep(3)

    procs = []
    for c in SCENARIOS[args.scenario]:
        wav = FIXTURES / c["wav"]
        if not wav.exists():
            print(f"missing fixture {wav} — run scripts/make_audio_fixtures.py")
            return 1
        procs.append(subprocess.Popen([
            str(VENV_PY), str(ROOT / "scripts" / "fake_caller.py"),
            "--room", room_name, "--identity", c["identity"], "--name", c["name"],
            "--wav", str(wav), "--delay", str(c["delay"]),
            "--linger", str(args.linger), "--record-agent",
            str(ROOT / "logs" / f"agent-audio-{call_id}-{c['identity']}.wav"),
        ]))

    for p in procs:
        p.wait()
    print("\nboth callers done; ending the call\n")

    if not args.keep_room:
        await gw.delete_room(room_name=room_name, call_id=call_id)
        # The worker's shutdown hook writes the transcript; give it a moment.
        await asyncio.sleep(3)

    transcript = Path(settings.log_dir) / f"{call_id}.transcript.json"
    if not transcript.exists():
        print(f"NO TRANSCRIPT at {transcript}")
        print("  is the worker running? check its stderr for stt.* events")
        return 1

    data = json.loads(transcript.read_text())
    utterances = data["utterances"]
    print(f"transcript: {transcript}  ({len(utterances)} utterances)\n")
    for u in utterances:
        mark = "  (unclear)" if u["confidence"] < 0.5 else ""
        print(f"  [{u['t_start_s']:7.2f}s] {u['speaker_name']:<7} "
              f"conf={u['confidence']:.2f}{mark}  {u['text']}")

    speakers = {u["speaker_id"] for u in utterances}
    ordered = all(
        utterances[i]["t_start_s"] <= utterances[i + 1]["t_start_s"]
        for i in range(len(utterances) - 1)
    )
    print(f"\nspeakers attributed : {sorted(speakers)}")
    print(f"time-ordered        : {ordered}")
    print(f"speakers expected   : {len(SCENARIOS[args.scenario])}")
    expected_speakers = len(SCENARIOS[args.scenario])
    ok = (len(utterances) > 0
          and len(speakers) == expected_speakers
          and ordered)
    print(f"\n{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
