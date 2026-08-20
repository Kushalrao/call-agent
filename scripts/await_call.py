#!/usr/bin/env python
"""Wait for the next call to finish, then print a diagnosis. Nobody watches logs.

Run it in the background before making a call. It notices a call log that did not
exist when it started, waits for that call to end, and prints the one screen that
answers "why didn't the agent do anything".

    .venv/bin/python scripts/await_call.py --timeout 1200
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from control_plane.config import get_settings  # noqa: E402

TERMINAL = {"agent.removed", "call.ended"}


def call_logs(log_dir: Path) -> set[Path]:
    return {p for p in log_dir.glob("*.jsonl") if not p.name.startswith("worker")}


def read(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def diagnose(rows: list[dict]) -> list[str]:
    """The specific question this exists to answer, stage by stage."""
    events = [r.get("event") for r in rows]
    out = []

    def had(*names: str) -> bool:
        return any(e in names for e in events)

    stages = [
        ("agent dispatched", had("agent.dispatched"),
         "control plane never dispatched — check DISPATCH_AGENT and restart it"),
        ("agent joined the room", had("agent.joined"),
         "the worker never picked up the job — is `python -m agent.worker dev` running?"),
        ("audio tracks subscribed", had("track.subscribed"),
         "the agent saw no audio tracks — nobody published a mic"),
        ("STT session opened", had("stt.session_open"),
         "Deepgram session never opened — check DEEPGRAM_API_KEY"),
        ("audio frames arrived", had("stt.first_frame"),
         "TRACKS SUBSCRIBED BUT ZERO AUDIO — the mic never reached the agent. "
         "This is an iOS audio-session problem, not an agent problem."),
        ("speech transcribed", had("aggregator.utterance"),
         "audio arrived but Deepgram returned nothing — wrong sample rate, "
         "silence, or a language/model mismatch"),
        ("a trigger fired", had("trigger.fired"),
         "nothing was judged worth acting on — see the classifier results below"),
        ("search ran", had("search.started"),
         "no destination resolved from what was said (see places.py table)"),
        ("agent spoke", had("tts.start"),
         "never got as far as speaking"),
    ]
    for label, ok, why in stages:
        out.append(f"  {'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"\n          -> {why}"))
        if not ok:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=1200.0)
    args = ap.parse_args()

    log_dir = Path(get_settings().log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    before = call_logs(log_dir)
    print(f"watching {log_dir}/ — make the call now ({len(before)} existing logs ignored)",
          flush=True)

    deadline = time.time() + args.timeout
    target: Path | None = None
    while time.time() < deadline:
        new = call_logs(log_dir) - before
        if new:
            # Track the newest, and keep re-picking: several short calls in a row
            # is the normal pattern when something is not working, and reporting
            # on the first attempt rather than the latest is actively misleading.
            newest = max(new, key=lambda p: p.stat().st_mtime)
            if newest != target:
                target = newest
                print(f"call started: {target.stem}", flush=True)
            rows = read(target)
            if any(r.get("event") in TERMINAL for r in rows):
                time.sleep(4)
                # If another call began while we waited, follow it instead.
                if max(call_logs(log_dir) - before,
                       key=lambda p: p.stat().st_mtime) == target:
                    break
        time.sleep(1)

    if target is None:
        print("\nNo call happened within the timeout.")
        return 1

    rows = read(target)
    t0 = rows[0]["ts"] if rows else 0.0
    print(f"\n{'='*66}\ncall {target.stem}   {rows[-1]['ts']-t0:.0f}s   {len(rows)} events\n{'='*66}")

    print("\n--- pipeline ---")
    for line in diagnose(rows):
        print(line)

    levels = [r for r in rows if r.get("event") == "stt.audio_flowing"]
    if levels:
        print("\n--- microphone levels (int16 peak; full scale 32767) ---")
        for r in levels[:6]:
            verdict = "SILENT — dead mic" if r.get("silent") else "audio present"
            print(f"  {str(r.get('speaker_id'))[:8]}  peak={r.get('peak'):>6}  {verdict}")
        if all(r.get("silent") for r in levels):
            print("\n  >> Every frame is silence. The phones publish a track that carries no")
            print("     audio. No speech-to-text vendor can transcribe this — the fault is")
            print("     in the iOS audio session, not in Deepgram.")
        elif any(not r.get("silent") for r in levels):
            print("\n  >> Real audio is arriving. If nothing was transcribed, the fault IS")
            print("     downstream in speech-to-text.")

    print("\n--- what was said ---")
    said = [r for r in rows if r.get("event") == "aggregator.utterance"]
    if not said:
        print("  (nothing transcribed)")
    for r in said:
        print(f"  {r.get('speaker_name','?')}: {r.get('text','<text logging off>')}")

    print("\n--- decisions ---")
    for r in rows:
        e = r.get("event")
        if e == "classifier.result":
            print(f"  classifier: {r.get('intent')} conf={r.get('confidence')} "
                  f"{r.get('reason') or ''}")
        elif e in ("trigger.fired", "trigger.suppressed"):
            print(f"  {e}: {r.get('trigger')} path={r.get('path')} {r.get('reason')}")
        elif e in ("search.started", "search.finished", "search.failed"):
            print(f"  {e}: {r.get('destination','')} "
                  f"{r.get('cheapest') or r.get('error') or ''}")
        elif e == "tts.start":
            print(f"  SPOKE: {r.get('text','<text logging off>')}")

    problems = [r for r in rows if r.get("level") in ("warn", "error")]
    if problems:
        print("\n--- warnings and errors ---")
        for r in problems:
            print(f"  [{r['level']}] {r['event']}: "
                  f"{str(r.get('error') or r.get('detail') or r.get('reason') or '')[:110]}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
