#!/usr/bin/env python
"""Read one call's logs and print what happened (spec section 11.2).

The per-call JSONL is the primary debugging artifact. This turns it into three
things worth looking at:

  timeline   every event in order, with the stage latency it carried
  transcript the call as the models see it — the exact render_window() output,
             overlap and confidence markers included
  health     per-speaker STT counts, reconnects, and anything degraded

    .venv/bin/python scripts/call_report.py                 # most recent call
    .venv/bin/python scripts/call_report.py <call_id>
    .venv/bin/python scripts/call_report.py <call_id> --emit-fixture
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.transcript import TranscriptAggregator  # noqa: E402
from control_plane.config import get_settings  # noqa: E402

# Events that carry no information once the ones around them are shown.
NOISE = {"livekit.skipped"}


def latest_call_id(log_dir: Path) -> str | None:
    calls = [
        p for p in log_dir.glob("*.jsonl")
        if p.name != "worker.out" and not p.name.startswith("worker")
    ]
    if not calls:
        return None
    return max(calls, key=lambda p: p.stat().st_mtime).stem


def load(path: Path) -> list[dict]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn final line from a crash is expected, not fatal
    return sorted(records, key=lambda r: r.get("ts", 0))


def rebuild_transcript(transcript_path: Path) -> TranscriptAggregator | None:
    """Replay persisted records through the real aggregator.

    Deliberately not a separate renderer: the report shows the same string the
    classifier and the reasoning turn are given, so a formatting bug is visible
    here rather than only in a model's behaviour.
    """
    if not transcript_path.exists():
        return None
    data = json.loads(transcript_path.read_text())
    agg = TranscriptAggregator(call_started_at=0.0)
    for u in data.get("utterances", []):
        agg.add(
            speaker_id=u["speaker_id"], speaker_name=u["speaker_name"],
            text=u["text"], t_start=u["t_start_s"], t_end=u["t_end_s"],
            confidence=u.get("confidence", 1.0),
        )
    return agg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("call_id", nargs="?", default=None)
    ap.add_argument("--emit-fixture", action="store_true",
                    help="write the transcript as a replay fixture for tests")
    args = ap.parse_args()

    log_dir = Path(get_settings().log_dir)
    call_id = args.call_id or latest_call_id(log_dir)
    if not call_id:
        print(f"no call logs in {log_dir}/")
        return 1

    log_path = log_dir / f"{call_id}.jsonl"
    if not log_path.exists():
        print(f"no log at {log_path}")
        return 1
    records = load(log_path)
    t0 = records[0]["ts"] if records else 0.0

    print(f"\n=== call {call_id} ===")
    print(f"{len(records)} events over {records[-1]['ts'] - t0:.1f}s\n")

    print("--- timeline ---")
    for r in records:
        if r.get("event") in NOISE:
            continue
        offset = r["ts"] - t0
        lat = f"  {r['latency_ms']:.0f}ms" if "latency_ms" in r else ""
        extras = " ".join(
            f"{k}={v}" for k, v in sorted(r.items())
            if k not in {"ts", "level", "service", "event", "call_id",
                         "latency_ms", "text"}
        )
        flag = "!" if r.get("level") in {"warn", "error"} else " "
        print(f"{flag} [{offset:7.2f}s] {r['event']:<26}{lat:>9}  {extras}")

    agg = rebuild_transcript(log_dir / f"{call_id}.transcript.json")
    if agg is not None:
        print(f"\n--- transcript as the models see it ({len(agg)} utterances) ---")
        # render(log), not render_window(): a report wants the whole call, and
        # the window is only ever what a model is shown mid-call.
        rendered = agg.render(agg.log)
        print(rendered or "(empty)")
    else:
        print("\n--- transcript ---\n(none persisted — did the worker shut down cleanly?)")

    print("\n--- health ---")
    per_speaker: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        sid = r.get("speaker_id")
        if sid:
            per_speaker[sid][r["event"]] += 1
    if not per_speaker:
        print("  no per-speaker events — the agent never attached to a track")
    for sid, counts in sorted(per_speaker.items()):
        parts = [f"{e.split('.')[-1]}={n}" for e, n in sorted(counts.items())]
        print(f"  {sid:<12} {' '.join(parts)}")

    # The two numbers every trigger budget in spec section 5 sits on top of.
    for field, label, note in (
        ("interim_lag_ms", "interim lag", "speech -> first partial text (fast path)"),
        ("finalize_lag_ms", "finalize lag", "speech end -> usable transcript"),
    ):
        vals = sorted(r[field] for r in records if field in r)
        if vals:
            p50 = vals[len(vals) // 2]
            print(f"\n  {label:<14}: p50 {p50:.0f}ms  max {max(vals):.0f}ms"
                  f"  ({note}, n={len(vals)})")

    errors = [r for r in records if r.get("level") in {"warn", "error"}]
    degraded = [r for r in records if r.get("event") == "stt.degraded"]
    print(f"\n  warnings/errors : {len(errors)}")
    print(f"  degraded STT    : {len(degraded)}")
    for r in errors:
        print(f"    ! {r['event']}: {r.get('error') or r.get('detail') or ''}")

    if args.emit_fixture and agg is not None:
        # Real transcripts become replay fixtures, so the decision layer can be
        # tested against things people actually said (spec section 11.3).
        out = ROOT / "tests" / "fixtures" / "transcripts" / f"{call_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"call_id": call_id, "utterances": agg.to_records()},
                                  indent=1))
        print(f"\nfixture written: {out.relative_to(ROOT)}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
