#!/usr/bin/env python
"""End-to-end Phase 0a smoke test against a running control plane.

Drives a whole call the way two phones will: log in with codes, open the ringing
WebSocket as the callee, place the call, receive the ring, accept, both report
joined, end. Then prints the per-call JSONL so you can see the event timeline.

    .venv/bin/uvicorn control_plane.main:app --port 8000   # in one shell
    .venv/bin/python scripts/smoke_call.py                 # in another
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import websockets

BASE = "http://127.0.0.1:8000"
WS = "ws://127.0.0.1:8000/v1/events"


def ok(label: str) -> None:
    print(f"  \033[32mPASS\033[0m {label}")


def fail(label: str, detail: str) -> None:
    print(f"  \033[31mFAIL\033[0m {label}: {detail}")
    raise SystemExit(1)


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=10) as c:
        health = await c.get("/healthz")
        if health.status_code != 200:
            fail("healthz", f"is the server running? got {health.status_code}")
        print(f"\nserver: {health.json()}\n")

        # --- login ---------------------------------------------------------
        codes = json.loads(Path("dev_codes.json").read_text())
        a = (
            await c.post(
                "/v1/auth/dev-login",
                json={"user_code": codes["caller_code"], "display_name": codes["caller"]},
            )
        ).json()
        b = (
            await c.post(
                "/v1/auth/dev-login",
                json={"user_code": codes["callee_code"], "display_name": codes["callee"]},
            )
        ).json()
        if "access_token" not in a or "access_token" not in b:
            fail("dev-login", f"{a} / {b}")
        ok(f"both users logged in ({codes['caller']}, {codes['callee']})")

        ha = {"Authorization": f"Bearer {a['access_token']}"}
        hb = {"Authorization": f"Bearer {b['access_token']}"}

        # --- callee opens the ringing socket -------------------------------
        async with websockets.connect(f"{WS}?token={b['access_token']}") as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            if hello.get("type") != "hello":
                fail("ws handshake", str(hello))
            ok("callee connected to ringing WebSocket")

            # --- place the call --------------------------------------------
            created = await c.post(
                "/v1/calls", json={"callee_id": b["user_id"]}, headers=ha
            )
            if created.status_code != 201:
                fail("POST /v1/calls", created.text)
            call = created.json()
            if call["state"] != "RINGING":
                fail("state after create", call["state"])
            ok(f"call created and RINGING ({call['call_id'][:8]})")

            # --- the ring arrives ------------------------------------------
            try:
                ring = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                fail("ring delivery", "no incoming_call within 5s")
            if ring.get("type") != "incoming_call" or ring.get("call_id") != call["call_id"]:
                fail("ring payload", str(ring))
            ok(f"callee received incoming_call from {ring['caller_name']}")

            cid = call["call_id"]

            # --- accept -----------------------------------------------------
            acc = await c.post(f"/v1/calls/{cid}/accept", headers=hb)
            if acc.status_code != 200 or acc.json()["state"] != "CONNECTING":
                fail("accept", acc.text)
            if not acc.json()["lk_token"]:
                fail("accept", "no LiveKit token returned")
            ok("callee accepted, got a LiveKit token, state CONNECTING")

            # --- both join --------------------------------------------------
            s1 = (await c.post(f"/v1/calls/{cid}/joined", headers=ha)).json()["state"]
            if s1 != "CONNECTING":
                fail("one-sided join", f"went to {s1} with only one party joined")
            ok("one party joined -> still CONNECTING (correct)")

            s2 = (await c.post(f"/v1/calls/{cid}/joined", headers=hb)).json()["state"]
            if s2 != "ACTIVE":
                fail("both joined", f"expected ACTIVE, got {s2}")
            ok("both parties joined -> ACTIVE (agent dispatch fires here)")

            # --- authorization spot check -----------------------------------
            leak = await c.get(f"/v1/calls/{cid}")  # no auth header
            if leak.status_code != 401:
                fail("unauthenticated read", f"expected 401, got {leak.status_code}")
            ok("unauthenticated call read rejected")

            # --- end --------------------------------------------------------
            end = await c.post(f"/v1/calls/{cid}/end", headers=ha)
            if end.json()["state"] != "ENDED":
                fail("end", end.text)
            ok("call ended")

    # --- the log artifact --------------------------------------------------
    log_path = Path("logs") / f"{cid}.jsonl"
    if not log_path.exists():
        fail("per-call log", f"{log_path} not created")
    lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    ok(f"{log_path} written ({len(lines)} events)")

    print("\n--- call timeline ---")
    t0 = lines[0]["ts"]
    for entry in lines:
        offset = f"+{(entry['ts'] - t0) * 1000:7.1f}ms"
        extra = " ".join(
            f"{k}={v}"
            for k, v in entry.items()
            if k not in {"ts", "level", "service", "event", "call_id"}
        )
        print(f"  {offset}  {entry['event']:<28} {extra}")

    print("\n\033[32mPhase 0a acceptance: all checks passed.\033[0m\n")


if __name__ == "__main__":
    asyncio.run(main())
