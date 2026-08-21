#!/usr/bin/env python
"""Generate two-speaker conversation WAVs using macOS `say`.

Real spoken audio, no recording session needed, and deterministic — so the same
fixture exercises STT the same way every run. Two distinct voices so the two
tracks are genuinely different speakers.

    .venv/bin/python scripts/make_audio_fixtures.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

OUT = Path("tests/fixtures/audio")

# A trip-planning conversation, because that is what the agent has to understand.
# Each entry is one speaker's whole side, spoken continuously; the two tracks are
# played with a stagger so they interleave.
SCRIPTS = {
    "rohan": (
        "Daniel",
        "So December is packed at work man. "
        "Yeah that works but keep it under thirty thousand each. "
        "Bali sounds good to me. What about flights though.",
    ),
    "kushal": (
        "Alex",
        "What about the second week of December instead. "
        "And direct flights only please. "
        "We fly out of Bangalore right.",
    ),
    # Direct address, so the fast path can be measured on real audio. The name
    # is spoken early: what matters is the gap between the word being said and
    # the trigger firing.
    "rohan_direct": (
        "Daniel",
        "Hey copilot, find us flights from Bangalore to Bali. "
        "Second week of December, under thirty thousand.",
    ),
    # The narrow first-loop test: wake name + a destination, nothing else.
    "rohan_flights": (
        "Daniel",
        "Hey copilot, find me flights to Singapore.",
    ),
    # No wake word anywhere: this is the ambient path. Two people planning a
    # trip, with the destination corroborated across utterances.
    "ambient_rohan": (
        "Daniel",
        "December is packed at work. Singapore sounds good to me though. "
        "Yeah let's do Singapore, second week of December.",
    ),
    "ambient_kushal": (
        "Alex",
        "What about the second week instead. Direct flights only please, "
        "under thirty thousand each.",
    ),
    "kushal_reply": (
        "Alex",
        "Yeah, direct ones only if you can.",
    ),
}


def build(name: str, voice: str, text: str) -> Path:
    aiff = OUT / f"{name}.aiff"
    wav = OUT / f"{name}.wav"
    OUT.mkdir(parents=True, exist_ok=True)
    subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
    # 16kHz mono 16-bit PCM — what the agent's STT pipeline consumes.
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)],
        check=True,
    )
    aiff.unlink(missing_ok=True)
    return wav


def main() -> int:
    if sys.platform != "darwin":
        print("needs macOS `say` and `afconvert`", file=sys.stderr)
        return 1
    for name, (voice, text) in SCRIPTS.items():
        wav = build(name, voice, text)
        import wave as w
        with w.open(str(wav)) as f:
            secs = f.getnframes() / f.getframerate()
        print(f"  {wav}  {secs:.1f}s  voice={voice}")
    print("\nfixtures ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
