# hands-free

A two-person voice call with an ambient AI agent in the room. The agent hears
both sides, keeps the context of what they're planning, and answers by rendering
a widget over the call UI and/or speaking back. Flight search is the v1 tool.

**Docs**
- [`docs/EXECUTION_PLAN.md`](docs/EXECUTION_PLAN.md) — the live checklist. Start here.
- [`docs/call-agent-pipeline-spec-v1.md`](docs/call-agent-pipeline-spec-v1.md) — the design spec.

---

## Setup

Requires Python 3.12 and [`uv`](https://docs.astral.sh/uv/). On macOS 26, `pip`
itself is broken (its vendored `truststore` can't parse the OS version, because
`platform.mac_ver()` returns empty strings) — use `uv`, which doesn't depend on it.

```sh
uv venv --python 3.12
uv pip install -e ".[dev]"
cp .env.example .env          # then fill in LIVEKIT_* to go live
```

## Run

```sh
.venv/bin/python scripts/seed_users.py Kushal Rohan    # prints login codes
.venv/bin/uvicorn control_plane.main:app --reload --port 8000
```

Then, in another shell:

```sh
.venv/bin/python scripts/smoke_call.py     # drives a whole call end-to-end
.venv/bin/python -m pytest -q              # unit + authorization tests
.venv/bin/python scripts/check_livekit.py   # verify LiveKit credentials work
```

`smoke_call.py` is the Phase 0a acceptance test: it logs in both users, opens the
ringing WebSocket, places a call, receives the ring, accepts, has both parties
report joined, ends the call, and prints the per-call event timeline.

To reach the server from a phone on the same Wi-Fi, bind to all interfaces
(`--host 0.0.0.0`) and point the app at your Mac's LAN IP.

## iOS app

Requires Xcode and [XcodeGen](https://github.com/yonaskolb/XcodeGen) (`brew install xcodegen`).
`project.yml` globs its sources, so **re-run `xcodegen generate` after adding a file.**

```sh
cd ios
xcodegen generate
open HandsFree.xcodeproj      # pick your signing team once, under Signing & Capabilities
```

A free Apple ID is enough to install on a physical device (7-day provisioning). TestFlight is
not required and neither is a paid account.

**On a phone**, set the server field on the login screen to your Mac's LAN address
(`http://192.168.x.x:8000`) and start the server with `--host 0.0.0.0`.

**On the Simulator**, CallKit is not dependable — `providerDidReset` fires right after
`reportNewIncomingCall` and would kill a live call, and PushKit doesn't work there at all. The
app therefore takes an in-app ringing path under `#if targetEnvironment(simulator)` and lets
LiveKit own the audio session; devices get the real CallKit path. Everything after "answer" is
shared code.

Two dev-only launch hooks (`#if DEBUG`) make the Simulator scriptable, since it can't be tapped:

```sh
xcrun simctl launch <sim-udid> com.handsfree.app \
  -control_plane_base_url "http://127.0.0.1:8000" \
  -auto_answer 1 \
  -session_token "<jwt>" -session_user_id "<uuid>" -session_user_name "Rohan"
```

Grant the mic once per simulator: `xcrun simctl privacy <sim-udid> grant microphone com.handsfree.app`

## Layout

```
control_plane/          FastAPI control plane
  config.py             all settings, from env
  logging_setup.py      structured events + per-call logs/{call_id}.jsonl
  models.py             users, calls
  security.py           session JWTs (same shape Apple auth will issue)
  livekit_gateway.py    token minting, room create/delete, agent dispatch
  events_hub.py         WebSocket ringing (dev transport for PushKit)
  ratelimit.py          per-caller AND per-callee limits
  routers/              auth, users, calls
scripts/
  seed_users.py         create dev users with typeable codes
  smoke_call.py         end-to-end acceptance
tests/                  unit + authorization tests
ios/
  project.yml           XcodeGen spec (re-generate after adding files)
  HandsFree/
    Config.swift        server address, persisted and editable in-app
    APIClient.swift     one method per control-plane endpoint
    EventSocket.swift   ringing WebSocket (PushKit replaces this in Phase 7)
    CallCenter.swift    CallKit + LiveKit — the core
    AppState.swift      session, contacts, socket-to-CallKit wiring
    Views.swift         login, contacts, call surface, debug overlay
```

## Without LiveKit credentials

The control plane runs fine: tokens come back as dev placeholders and room
create/delete/dispatch are no-ops, logged as `livekit.skipped`. Add `LIVEKIT_*`
to `.env` and the identical code path goes live. The tests deliberately run in
this mode so CI needs no credentials.

## Things that must not regress

1. **The agent may fail; the call may not.** Every agent failure degrades to a
   working phone call.
2. **Authorization on every `call_id` endpoint.** Without it a `call_id` is a
   room key. See `tests/test_call_lifecycle.py::test_third_party_cannot_see_or_join_a_call`.
3. **Tokens are minted server-side only.** Never on device.
4. **Humans cannot publish data messages** (`canPublishData: false`), so the
   widget channel is agent-to-client only and a client can't fabricate a widget.
5. **No raw audio is ever persisted.**
6. **No write-capable agent tool without a security review.** The call transcript
   is untrusted input.
