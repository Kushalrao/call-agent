# hands-free — Execution Plan

**Created:** 2026-08-20 · **Owner:** Kushal · **Source of truth for sequencing.**
**Status:** ✅ Phase 0 complete (two-way audio, two iPhones). Phase 1 built (widget renders in-call, verdict pending).
**Flight search done early and parked** — all 3 platforms live via the Watermelon extension, ~16s. See `watermelon-inventory.md`.
**Next: Phase 3 — the decision layer.** Phase 2 is built and passing end to end against live LiveKit + Deepgram; see the measured-latency note in Phase 2 before designing Phase 3, because the numbers are not what the spec assumed.
Design detail lives in [`call-agent-pipeline-spec-v1.md`](./call-agent-pipeline-spec-v1.md). This document is the checklist. When the two disagree about *order*, this one wins; when they disagree about *design*, amend the spec.

## How to use this

- Check items off as they land. A phase is done only when **every** acceptance criterion passes — not when the code exists.
- Each phase has an **Acceptance** block. Those are demos, not opinions: either the thing happens or it doesn't.
- `⚠️ RISK` items are things that could invalidate a later phase. Resolve them early and cheaply.
- Don't reorder Phase 1 later. It is deliberately early because it is the only phase that can tell you the product doesn't work.

**Legend:** `[ ]` todo · `[x]` done · `[~]` in progress · `[!]` blocked

---

## Phase −1 — Pre-flight (half a day, do before writing real code)

Two unknowns can force redesign. Both are cheap to check now and expensive to discover in week 6.

- [ ] `⚠️ RISK` **Curl the flight API.** Measure p50/p95 latency over ~20 real queries. Does it expose place resolution (city string → IATA), or do we need the static top-200 table? Does it return deep links?
  - *Why now:* its latency determines whether speculative prefetch (§6) is a nice-to-have or load-bearing, and place resolution determines whether §6's fallback table is on the critical path.
- [ ] `⚠️ RISK` **Read the current LiveKit Agents docs and pin the version.** Confirm how to subscribe to two remote audio tracks in a job entrypoint *without* `AgentSession`'s conversational turn loop, and whether the ambient pattern means bypassing `AgentSession` almost entirely.
  - *Why now:* the spec warns the 1.x API surface moved. Trust the warning, not the version number. Getting this wrong costs a week of fighting the framework's one-human assumption.
- [ ] Create accounts and put keys in `.env`: LiveKit Cloud, Deepgram, Anthropic, ElevenLabs, flight API.
  - LiveKit: sign up at cloud.livekit.io -> create a project -> generate API key/secret on its settings page -> copy the `wss://...livekit.cloud` URL from the top of the dashboard. Verify with `.venv/bin/python scripts/check_livekit.py` (mints a token, creates/lists/deletes a room).
- [ ] Turn on zero-retention / no-training settings wherever each vendor offers them, and record what was set in `SECURITY.md`.

**Acceptance:** flight API p50 latency written down here → `______ms`. LiveKit Agents version pinned in `requirements.txt`. A `.env.example` exists listing every key by name.

---

## Phase 0 — Control plane + iOS call MVP (week 1)

Pure plumbing, zero product risk. Two people can talk to each other.

### 0a. Control plane (FastAPI) — ✅ DONE 2026-08-20
- [x] Project skeleton (`uv` + Python 3.12), config from env, schema (`users`, `calls`)
- [x] **Structured JSON logging from line one** (§11.1) — canonical `Events` class, per-call `logs/{call_id}.jsonl`, `Timer` for `latency_ms`, `text=` privacy gate
- [x] `POST /v1/auth/dev-login` → session JWT; rate limited 5/min/IP; only when `AUTH_MODE=dev`
- [x] `scripts/seed_users.py` — human-typeable codes (`KUSHAL-W2T`), also writes `dev_codes.json`
- [x] `GET /v1/users/contacts` (with online flag from the WS hub)
- [x] `POST /v1/calls` · `/accept` · `/decline` · `/end` · `/joined` · `/token` · `GET /{id}` with the state machine (§1.3)
- [x] **Authorization checks** — non-participants get 404 (not 403, which would confirm the call exists); only the callee may accept/decline; either party may end
- [x] **Rate limit `POST /v1/calls` per caller AND per callee**
- [x] LiveKit token minting server-side (§1.4), `canPublishData: false` for humans; agent token carries `kind=agent` metadata
- [x] `GET /v1/events` WebSocket for ringing (§2.2b), multi-socket per user
- [x] 45s ring timeout via a restart-surviving sweeper
- [x] `POST /calls/{id}/token` re-mint — covers the "token expiry pre-join" row of §10; refused on a terminal call
- [x] 17 tests passing, `scripts/smoke_call.py` green end-to-end (login → WS ring → accept → both joined → ACTIVE → end)
- [ ] **Alembic migrations** — currently `create_all()`, which is dev-only. Needed before the first deploy holding data anyone cares about.
- [ ] Swap SQLite → Postgres (`uv pip install -e ".[postgres]"`, change `DATABASE_URL`). Models are already Postgres-compatible; `UTCDateTime` normalises the naive/aware difference between the two.
- [ ] Replace in-memory rate limiting with Redis before running more than one replica (limits currently multiply by replica count)
- [ ] Replace `/joined` with LiveKit `participant_joined` webhooks once there's a public URL

### 0b. iOS app (SwiftUI) — built 2026-08-20, Simulator-verified
- [x] Xcode project via **XcodeGen** (`ios/project.yml`) — regenerate with `xcodegen generate` after adding files. LiveKit Swift SDK **2.16.0** via SPM. Background Modes (voip, audio), `NSMicrophoneUsageDescription`, `NSAllowsLocalNetworking` for the LAN dev server.
- [x] Dev-login screen (name + code + editable server address, since the LAN IP changes)
- [x] Contacts list with online dots → tap to call
- [x] CallKit outgoing (`CXStartCallAction`) and incoming (`reportNewIncomingCall`), driven by the WebSocket; the reporting path is factored so the Phase 7 PushKit handler calls the identical method
- [x] Audio session: category set on answer/start, **never `setActive(true)`**. LiveKit's `isAutomaticConfigurationEnabled = false` and `setEngineAvailability(.none)` until `provider(_:didActivate:)` — LiveKit otherwise owns AVAudioSession and fights CallKit for it, which is how you get one-way or dead audio.
- [x] Accept order: `CXAnswerCallAction` → `/accept` → `room.connect()` → publish mic → fulfil
- [x] Token refresh on a lapsed 120s join window, with one retry before failing
- [x] Reconnect pill via `roomIsReconnecting` / `roomDidReconnect` — a blip shows a pill, never ends the call
- [x] Agent indicator driven by participant metadata (`kind=agent`), not local state; not a toggle
- [x] Debug overlay (§11.2), `#if DEBUG` only — live event log + connection quality
- [ ] Select your signing team in Xcode once (free Apple ID is enough for direct-to-device; no TestFlight needed)

**Simulator caveat, found the hard way.** CallKit is not dependable on the Simulator: `providerDidReset` fires spontaneously right after `reportNewIncomingCall` and would kill a good call (PushKit doesn't work there either). So the app uses `#if targetEnvironment(simulator)` to take an in-app ringing path and let LiveKit own the audio session, while devices get the real CallKit path. Everything after "answer" is shared code, so the Simulator still exercises what matters. `providerDidReset` is also now guarded to ignore the normal launch-time reset.

**Dev test hooks** (both `#if DEBUG`): launch arguments seed `UserDefaults`, so a session can be injected without typing, and `-auto_answer 1` makes the app answer itself — the Simulator can't be tapped from a script, and the Phase 2 fake-caller harness will need the same hook.

```sh
xcrun simctl launch <sim> com.handsfree.app \
  -control_plane_base_url "http://127.0.0.1:8000" -auto_answer 1 \
  -session_token "<jwt>" -session_user_id "<uuid>" -session_user_name "Rohan"
```

**Acceptance**
- [x] `logs/{call_id}.jsonl` exists with `call.created` → `call.ringing` → `call.accepted` → `call.active` → `call.ended`
- [x] A non-participant cannot touch a call by id
- [x] Session restores, contacts load, ringing WebSocket connects (`ws.connected` in the server log)
- [x] A call rings the app; the incoming-call screen appears with the caller's name
- [x] Answering fetches a token, joins the **real LiveKit room**, and publishes a microphone track (`tracks=['0/2']` = audio/microphone confirmed via `list_participants`)
- [x] Both parties reporting joined moves the server to **ACTIVE**
- [x] No APNs, no TestFlight, no paid Apple Developer account required
- [x] **Audio flows both ways** — verified on two physical iPhones, 2026-08-20
- [x] CallKit's native call screen appears on a real device
- [ ] The call survives a 5s network drop and shows the reconnecting pill (not yet exercised)

**Network gotcha, cost an hour:** the usual Wi-Fi has **client isolation** (~92 devices on a /22; the Mac cannot ping any of them), so phones cannot reach the control plane on the Mac and no firewall change helps. Two-device testing runs over a **Personal Hotspot** — the Mac lands on `172.20.10.x`. Verify with a real login over that address before typing it into the phones.

**iOS crash, cost twenty minutes:** `__abort_with_payload` at launch on device. LiveKit's WebRTC enumerates capture devices even in audio-only mode, and iOS kills the process the moment a protected resource is touched with no usage description — it does not wait for a prompt. Fixed by declaring `NSCameraUsageDescription` and the Bluetooth keys, and by moving audio-stack setup out of `CallCenter.init()` (it ran at app launch) into a lazy `prepareAudioStack()` on first call.

**Verified against live LiveKit (project `hands-free-dev-wusm4l50`)** — `scripts/check_livekit.py` passes all 5 checks; `smoke_call.py` drives a full call through real rooms and real tokens.

**Measured finding: LiveKit Cloud room create/delete costs 1.4–2.6s from here** (consistent; not a cold start, unaffected by client reuse). That was sitting in front of the ring, so the callee's phone would not have buzzed for ~3 seconds. Fixed by moving every slow LiveKit call off the request path:
- Room creation is backgrounded and *tracked*; ring latency went 2738ms → **1.5ms**
- `delete_room` and `update_room_metadata` await the tracked create before running, so a call shorter than the create latency no longer fails its delete and leaks a room
- Agent dispatch is backgrounded — the humans are already talking and must never wait on the agent
- `empty_timeout=300` must stay above the 45s ring timeout or a room could be reaped mid-ring

- [ ] Check the LiveKit project region in the dashboard — if it isn't the closest one, 1.4–2.6s may partly be routing. Worth one look, but the architecture no longer depends on it.

---

## Phase 1 — Widget feel spike (2 days, week 2) 🔴 highest product risk

The only phase that can kill the idea. A card animating over a live call either feels magic or feels like a notification, and that is a pure client-side question answerable with hardcoded data.

- [x] `flight_results` card rendered over the live call surface (`WidgetViews.swift`)
- [x] Fired by debug buttons in the call screen — airplane = flight card, magnifying glass = searching toast. No agent, no model. Also scriptable via `-auto_widget 1`.
- [x] Bottom sheet: springs in, swipe-down or X to dismiss, auto-expires after `ttl_s`
- [x] **Real spec §7 envelope** (`v`, `widget_id`, `type`, `ttl_s`, `payload`), decoded by a two-pass decoder so an **unknown `type` is dropped and logged, never an error** — forward compatibility is tested, not assumed
- [x] **Real LiveKit data-message transport already wired** (`didReceiveData`, topic `"widget"`, agent-metadata checked). Phase 4 only has to start *sending* — nothing on the client changes.
- [x] `agent_status` toast for the "searching flights…" case
- [x] Widget dismissals logged, so dismiss rate is measurable from day one
- [ ] Try it during a **real** two-person call, not the Simulator

Two layout bugs found and fixed by looking at it rather than reasoning about it: a flight with no booking link was rendering dimmed (`.disabled()` greys the whole row, including the price — now `.allowsHitTesting`), and the card could overlap the hang-up button (it is now laid out *above* the controls rather than overlaid with a guessed padding, because the card's height depends on how many flights came back).

**Acceptance**
- [ ] Kushal's own verdict, written down here: does a card appearing mid-call feel like help or like an interruption? → `__________`
- [ ] If it feels wrong: stop and redesign the surface before building anything in Phase 3–5.

---

## Phase 2 — Agent joins, ears only (weeks 3–4) ✅

No decisions, no output. Just: does it hear both people correctly? It does.

- [x] LiveKit Agents worker, explicit dispatch from the control plane when the call hits `ACTIVE`
- [x] Subscribe to both remote audio tracks separately; one Deepgram streaming session per participant track (**never mix and diarize**)
- [x] Deepgram config: `nova-3`, `language="multi"` (Hindi–English code-switching), interim results ON, smart formatting ON, endpointing 300ms
- [x] **Keyterm prompting for place names** — not in the original plan. The first rehearsal transcribed "Bali" as "Boli" and dropped "Bangalore" entirely. A wrong city is a wrong flight search, so `agent/vocabulary.py` biases the model toward the origins, destinations and carriers that actually come up. Both words transcribe correctly now.
- [x] STT reconnect: 15s PCM ring buffer, replay on reconnect, 3-failures-in-30s → mark degraded. **The call must continue regardless.**
- [x] Transcript aggregator (§4.2) — one shared monotonic clock, `stream_epoch` per stream, bisect insert by `t_start`
- [x] `render_window()` with the exact documented format (timestamps, speaker names, `(overlapping)`, `(unclear)`)
- [x] Persist the full speaker-attributed transcript (§9 as amended). 30-day TTL still to wire — the file is written, nothing reaps it yet.
- [x] `scripts/fake_caller.py` — joins a room and publishes a WAV as its mic, and reports its own realtime drift
- [x] `scripts/rehearse_call.py` — creates the room, dispatches the agent, runs both callers, asserts attribution + ordering
- [x] `scripts/make_audio_fixtures.py` — generates two-speaker fixtures with macOS `say`, two distinct voices
- [x] `scripts/call_report.py` — timeline with per-stage latency, the transcript **as the models see it**, per-speaker health, `--emit-fixture`
- [ ] More fixture conversations, including Hindi–English code-switching (one two-speaker English fixture exists)

### Unit tests (pure data, no audio, no network) — all required
- [x] Interleaved ordering across two streams
- [x] Heavy overlap preserved as overlapping intervals (never merged or serialized)
- [x] Reconnect with buffer replay (offsets correct after `stream_epoch` reset)
- [x] Low-confidence `(unclear)` rendering
- [x] Window eviction (by age, by count, and at the boundary)
- [x] Deterministic `render_window()` snapshot
- [x] `call_id` extraction and path-segment sanitizing (see the bug note below)

**Acceptance**
- [x] Two fake callers produce a correctly attributed, correctly ordered transcript with overlaps intact — `rehearse_call.py` asserts this and prints PASS
- [ ] Same verified on a live two-phone call via `call_report.py`
- [ ] An STT socket killed mid-call recovers, and the humans notice nothing (code path built, not yet fault-injected)
- [x] Every §11.1 canonical event for this stage is emitted, with `latency_ms` / `interim_lag_ms` / `finalize_lag_ms`

### ⚠️ Measured latency — read this before designing Phase 3

Against live LiveKit Cloud (India South) and Deepgram, five utterances:

| | p50 | max | what it is |
|---|---|---|---|
| `interim_lag_ms` | **~500ms** | 780ms | speech → first partial text. The fast path. |
| `finalize_lag_ms` | **~1700ms** | 2560ms | speech end → aggregated, usable transcript. |

Both are far above the spec's assumptions (§5 budgets a ~150ms fast path and a
~550ms semantic path). Ruled out as a test artifact: the fake caller reports
−1.00s drift, i.e. it publishes *ahead* of realtime, so it cannot be inflating
these. The likely dominant term is geography — the worker is in India South and
Deepgram is US-hosted.

What this changes for Phase 3:

- The **fast path stays the right design**, and is now load-bearing rather than an
  optimization: at a ~1.7s finalize lag, anything that waits for FINAL cannot feel
  responsive to a direct address.
- **"Wake word < 200ms" is not achievable on this path** and should be restated as
  a budget over our own processing, measured from interim receipt, not from the
  spoken word. The end-to-end figure is ~500ms + our processing.
- A ~2.2s total for the semantic path is **acceptable for ambient noticing** ("they
  have settled on Bali in December" → prefetch) and **not** acceptable for direct
  address. That asymmetry is exactly the two-speed split, so the architecture holds.
- Worth trying before accepting these numbers: a Deepgram region closer to the
  worker, or co-locating the worker with Deepgram rather than with LiveKit.

### Bugs the first live run found (all fixed, all now tested)

1. **`call_id` was never parsed out of dispatch metadata.** Room metadata had not
   reached the process at `entrypoint`, so the job-metadata fallback was used as a
   raw string — and the transcript landed in a file literally named
   `{"call_id":"rehearse-ef1a5f18"}.transcript.json`. Both sources are now parsed,
   room metadata wins, and it is re-read after `connect()`.
2. **`call_id` became a filename unsanitized.** It comes from room metadata, which
   is data from outside the process. Now constrained to one path segment in both
   the worker and the log sink, so `../../etc/passwd` cannot steer a write.
3. **The stream clock started at STT session open, not at the first frame.** A
   track can be subscribed well before it delivers audio; every timestamp on that
   stream was offset by the gap.
4. **Provider keys were in `.env` but not in `Settings`,** and `.env` never reaches
   `os.environ` — so the plugin's implicit key lookup would have failed at the
   first utterance of a live call. `config.py` is now the only `.env` reader, and
   the worker refuses to start without a key rather than going deaf mid-call.
5. **`call_report.py` rendered a 4-utterance transcript as empty** — `window(now=inf)`
   with `window_seconds=inf` gives `inf - inf = nan`, and every comparison against
   nan is false. A report wants the whole call, so it calls `render(log)`.

### Running it

```bash
# terminal 1
LOG_SERVICE=agent-worker LOG_TRANSCRIPTS=true .venv/bin/python -m agent.worker dev

# terminal 2
.venv/bin/python scripts/make_audio_fixtures.py   # once
LOG_TRANSCRIPTS=true .venv/bin/python scripts/rehearse_call.py
.venv/bin/python scripts/call_report.py --emit-fixture
```

---

## Decisions taken 2026-08-21 (voice path)

**TTS: ElevenLabs Flash v2.5.** Needs `ELEVENLABS_API_KEY` — currently an empty
placeholder in `.env`. Deepgram Aura-2 was offered as the cheaper start (it works
on the existing STT key today, first byte 1.07s over non-streaming REST from
India) and stays the documented fallback, since `deepgram.TTS` is already in the
installed plugin.

**Proactive triggers speak.** The agent announces a result as it renders it,
rather than rendering silently. This voids the spec's "discard proactive text
before TTS" rule — see the 2026-08-21 amendment in §5.3 for what that costs and
the three mechanisms it promotes to load-bearing (2-minute proactive speech rate
limit, mandatory hold-and-expire, and a `TripContext` confidence gate that
suppresses *speech* while still rendering the *widget*).

**Off-tool questions ("is Bali good in December?") — deferred.** Not built in
Phase 3. Until it is decided, the agent stays silent on anything it has no tool
for, which is the safe default rather than a chosen behaviour.

### Keys — both supplied 2026-08-21, both verified

- [x] `ANTHROPIC_API_KEY` — works. `claude-haiku-4-5`, `claude-sonnet-5`, `claude-opus-5` all available.
- [x] `ELEVENLABS_API_KEY` — works, but it is a **free-tier** key with two live constraints:
  - It is **scoped**: no `user_read` or `voices_read`, so quota and the voice list cannot be queried programmatically. Good practice; just means those checks fail rather than the key being broken.
  - **Free accounts are refused library voices via the API** (`402 paid_plan_required`). Probed five voice IDs; four work. `EXAVITQu4vr4xnSDxMaL` is now pinned in `.env` (first byte 592ms, the fastest of the four).
  - Free tier is ~10k characters/month — roughly **100 agent remarks total**. That is a testing constraint worth knowing before we start iterating on voice.

### Measured: the model layer (n=8 plus a live call, 2026-08-21)

| | value | note |
|---|---|---|
| classifier latency | **p50 1050ms**, max 1400ms | fits the 1.5s spec timeout, ~100ms headroom at max |
| classifier cold start | **2041ms** | exceeds the timeout — this is why prewarm is mandatory, now confirmed empirically |
| classifier tokens | 579–688 in / 19 out | grows with the rolling window |
| classifier cost | **$0.00067–0.00078 per utterance** | → **~$0.081 per ten-minute call** |
| reasoning turn (est.) | ~$0.0075 per turn | Sonnet 5, ~1500 in / 200 out |
| ElevenLabs first byte | 592–731ms | same geography tax as STT |

**Prompt caching does not apply, and this was checked rather than assumed.** The
system prompt is ~380 tokens; Haiku's minimum cacheable prefix is 2048, so
`cache_creation` and `cache_read` both come back **zero**. An earlier estimate in
this document assumed caching would roughly halve classifier cost — it will not.
Padding the prompt to reach the minimum would enable it and save a fraction of a
cent while adding tokens to every request; not worth it. `Usage.cache_hit_ratio`
is logged on every call so this stays visible instead of being assumed again.

**The semantic path lands ~2.75s after speech ends** (1700ms finalize + 1050ms
classifier). Acceptable for ambient noticing, which is what it is for. Direct
address goes through the fast path instead.

### Spend controls — `agent/budget.py`

The risk is not steady-state cost, it is a loop. So every ceiling is a
**refusal**, not a warning:

| Layer | Default | Env |
|---|---|---|
| model calls per call | 150 | `LLM_MAX_CALLS_PER_CALL` |
| USD per call | 0.50 | `LLM_MAX_USD_PER_CALL` |
| USD per day | 10.00 | `LLM_MAX_USD_PER_DAY` |
| kill switch | on | `LLM_ENABLED=false` |
| spend-nothing dev mode | off | `LLM_OFFLINE=true` |

- The daily ledger is **persisted to `logs/llm_spend.json`**, because a daily
  ceiling exists to stop the exact thing that would blow through it — a process
  that keeps crashing and restarting. An in-memory counter resets on every
  restart, which is backwards.
- The ceiling is checked **before** the request with the estimated cost included,
  so an over-budget call is never sent.
- An unknown model string is priced at the **most expensive** known rate, so a
  typo shows up as a scary number rather than as free.
- Every call logs `llm.call_total`, so cost is visible per call rather than as a
  surprise at the end of the month.

### Built and live-verified

- [x] `agent/classifier.py` — Haiku 4.5, schema-enforced output, 1.5s timeout, prewarm, budget-gated. **Every failure path returns `none`** — a classifier that is down means an agent that does not volunteer, never a call that breaks.
- [x] `agent/budget.py` — the ceilings above, 18 tests.
- [x] Wired into the worker: `prewarm_fnc` at process boot, classify on every FINAL, policy decides, budget recorded on fire.

Observed on a live call, and this is the design working rather than a lucky run —
the confidence gradient rises as the conversation gets concrete, and the gates
sit exactly on it:

```
utterance 1  "December is packed at work"     -> none
utterance 2  confidence 0.75                  -> suppressed  low_confidence
utterance 3  confidence 0.85                  -> fired       widget only
utterance 4  confidence 0.85                  -> suppressed  cooldown:17.5s
```

### What a real call does today, end to end

Verified on the `direct` rehearsal scenario:

```
"Hey copilot, find us flights from Bangalore to Bali"
  -> fast path     trigger.fired  path=fast  matched="co pilot"     ~650ms
  -> widget        "One moment…"          (both participants)
  -> semantic path trigger.fired  path=semantic  confidence=0.95    ~1100ms
  -> widget        "Checking flights…"    (both participants)
"Second week of December, under 30,000"
  -> trigger.suppressed  busy:processing      <- joins the turn, does not restart it
```

Still missing between there and a finished answer: the reasoning turn, the
`flight_results` widget, the voice path, and the flight-search tool wiring.

### Bugs the live model runs found

6. **`output_config` schemas reject `minimum`/`maximum` on number types** (400,
   `"For 'number' type, properties maximum, minimum are not supported"`). Every
   classification on a live call failed. Worth noting what happened next: all five
   degraded to `none`, the call completed clean, and **$0.00 was spent** — failed
   requests are not billed. The safety design held under a real failure. The range
   is now enforced by clamping in `parse_classification`.
8. **The wake name was missing from the STT keyterms.** A live call transcribed
   "Hey copilot" as **"Echo Pilot"**, so the fast path never matched and a direct
   question went unanswered. Every other keyterm only affects *what* the agent
   does; this one decides *whether it reacts at all*. Now the first entry in
   `vocabulary.py`, and it transcribes correctly.
9. **The phase guard was decorative.** `evaluate()` refuses while PROCESSING, but
   nothing ever *set* PROCESSING. A live call fired three `direct_address`
   triggers in six seconds — one question plus two added constraints — and the
   agent would have answered the same request three times. Added
   `Policy.begin()`/`complete()`.
10. **Then the mirror of it:** the fast path claimed the turn and never released
   it, so after one wake word the agent was trigger-deaf for the rest of the
   call — no error, no log, just silence. Both paths now release. A test asserts
   that an unmatched `begin()` is visible.
11. **The classifier was judging the window, not the utterance.** Three
   consecutive lines came back `direct_address` at 0.95, including "Second week
   of December, under 30,000", because the whole window went over with a prose
   instruction to judge "the most recent". The target is now tagged in
   `<classify>` with the rest in `<context>`, and the prompt says explicitly that
   a follow-up constraint is not a fresh address.
7. **The rate limits were inert in the live path.** `evaluate()` is a pure query
   and nothing called `record_action()`, so a real call fired four proactive
   triggers in twenty seconds while every unit test passed. Budgets are now spent
   at the moment of firing, and a regression test reproduces the original
   four-fire sequence.

### Knowledge sources — there is no knowledge base, by design

| Priority | Source | Used for |
|---|---|---|
| 1 | The call transcript | What they want. The product thesis. |
| 2 | Tool results (flight search) | **Every travel fact.** Prices, times, carriers, availability. |
| 3 | Claude's parametric knowledge | Language and light inference only — "Bali → DPS". Never a price, schedule, or availability claim. |

The invariant: **the agent never speaks a fact it did not get from a tool.** The
one thing the model authors is the `TripContext` (§4.3) — inferring
`Bangalore → Bali, 2nd week December, under 30k, direct only` out of an
overlapping conversation. Everything downstream rests on it, and now that
proactive triggers speak, a wrong inference is spoken aloud. That is the risk
surface worth the care, not the choice of voice.

---

## 🎉 WORKING ON REAL PHONES, REAL VOICES (2026-08-21)

Two iPhones on a hotspot, two humans talking, agent in the call:

```
+13.8s  "Hello."                        -> none
+19.4s  "Hi. Copilot."                  -> direct_address 0.85  -> toast
+21.7s  "Find me flights to Singapore." -> direct_address 0.95  -> search BLR->SIN
+32.4s  search finished  Rs 19,532  Cleartrip  9.7s
+38.2s  SPOKEN: "Cheapest to Singapore is Air India at Rs 19,532,
                 one stop, on Cleartrip."
```

**Measured end to end: 16.5s** from the request to hearing the answer
(finalize ~1.7s + classifier ~1.0s + search 9.7s + TTS first byte 0.97s).

### What the microphone levels settled

Deepgram was suspected and was **not** at fault. Peak int16 levels came back
`32595`, `15568`, `32768` — real audio, never silent. The earlier zero-transcript
calls were a dead mic, fixed by configuring the audio session on the in-app path.
No STT vendor change was needed. (Worth recording: the ElevenLabs key cannot do
STT anyway — `401 missing_permissions` on `speech_to_text`.)

### 🔴 Near-miss to fix before anyone else uses this

```
+26.7s  "Fine. Flies to Srinagar."   -> classifier: none (0.92)
```

STT mangled an utterance into a sentence naming **Srinagar**, which is a real
entry in the `places.py` table (SXR). The only thing that stopped a confident
search for flights to Srinagar was the classifier judging it unaddressed. That is
the guardrail working, but it is one judgement away from a wrong answer spoken
aloud.

A single mangled utterance should not be able to pick a destination. The fix is
to require the destination to survive a second signal — repeated across two
utterances, or confirmed against the extracted `TripContext` — rather than
trusting whatever the last transcript happened to say.

### Diagnostics built along the way

- `scripts/await_call.py` — waits for the next call, then prints a stage-by-stage
  pass/fail down the whole pipeline with the specific cause at the first failure,
  plus mic levels, what was said, every classifier verdict, and all errors.
  Nobody should have to tail a log to find out why the agent stayed quiet.
- Per-track **peak amplitude** logging with `silent: true/false`. This is what
  distinguished "a dead mic" from "broken STT" — indistinguishable in every other
  log line, and the two have nothing in common as fixes.
  First reported at 2s (not 10s): every real call was under fifteen seconds, so a
  ten-second cadence recorded nothing at all.

---

## ✅ The first end-to-end loop WORKS (2026-08-21)

Proven on a live call: spoken request → real local search → the agent speaks the
answer into the room.

```
"Hey copilot, flights to Bali."
  fast path    trigger.fired  path=fast  matched=copilot
  widget       "One moment…"  ->  both participants
  search       BLR -> DPS, 2026-08-31 (today + 10)
  spoken       "Cheapest to Bali is AirAsia at ₹31,679, one stop, on Cleartrip."
```

And on a second route, end to end in 11.8s:
`"Cheapest to Dubai is Air-India Express at ₹28,029, one stop, on Ixigo."`

**The agent speaks server-side into the LiveKit room, not via the phone.** One
audio track published at join and silent until used, so speaking costs no
renegotiation. Both humans hear it, which is the point.

Measured: **TTS first byte 506ms warm / 2265ms cold. Search 6.7–11.8s** with
`wait_for_all=False`.

### Deliberately omitted from the spoken sentence: departure times

Ixigo publishes `Z` (UTC) and Cleartrip publishes local+offset. Carrier, price and
stops are sourced identically across platforms and are safe to say; a time is not.
Saying "departs 2:40am" for an 8:10am flight is worse than a wrong card, because
**a misheard time cannot be re-checked.** Times go in once the normalization is
fixed — avoiding the bug beat rushing a fix for it.

### Bugs this loop found

12. **Chrome was a child process of the agent worker.** `subprocess.Popen` without
   `start_new_session`, so the browser died on every worker restart — and a
   restarted Chrome has a **cold session**, which is exactly the state in which
   the aggregators time out. Warmth is the entire reason that profile is
   persistent, and a browser that keeps being killed can never accumulate any.
   Now detached, and `dev_up.sh` owns its lifecycle.
13. **A missing extension looked like "no flights found".** `run_search` checked
   `result.record` and ignored `result.ok`, so a wrong `WATERMELON_EXTENSION_DIR`
   produced 0 options in 0.0s and the agent said "I couldn't find flights to
   Bali" — technically true, and it hid the misconfiguration completely. Failures
   are now surfaced, and "couldn't search" (our problem) says something different
   from "didn't find any" (a fact about the world).
14. **`.env` still wasn't reaching `flight_scout`.** Third instance of the same
   trap: `config.py` is the only `.env` reader, so any module using
   `os.environ.get` never sees it. The extension path is now passed explicitly.
15. **The classifier timeout was too tight, and it silently dropped the whole
   trigger.** A live call timed out at 1502ms against the spec's 1.5s. Raised to
   2.5s on measurement (p50 1050ms / max 1400ms) — see the §5.1b amendment.
16. 🔴 **The design mistake: I made the search depend solely on the classifier.**
   Both single-path failures have now been seen live — STT rendered "Hey copilot"
   as **"Alert."** and only the classifier caught it; then the classifier timed
   out and only the wake word caught it. Either path alone drops roughly one
   request in three. The fast path now drives the search directly and the
   classifier is a parallel route to the same action, never a gate on it. This is
   what §5.1a's "may only ADD a trigger, never suppress one" actually requires.
17. **The MV3 service worker sleeps in a fresh browser.** The extension only wakes
   once a page its manifest matches has loaded, so a just-started Chrome answers
   nothing and the search times out 45s later with no hint that the extension was
   simply asleep. `_warm_browser` now navigates a matched page first.

18. 🔴 **The agent joined with no participant metadata, so every widget would
   have been dropped on the phone.** `CallCenter.swift` verifies that widgets came
   from the agent by looking for `"kind":"agent"` in the publisher's participant
   metadata. `mint_agent_token` sets it — but the worker never uses that token,
   because the Agents framework mints its own on connect. Nothing errored on
   either side: the app would simply log "widget from non-agent participant,
   ignored" and show nothing, for the whole call.

   **My test harness could not have caught this**, because the fake caller read
   widgets without checking the publisher — it was more permissive than the real
   client. It now mirrors the client's gate exactly and prints
   `WIDGET WOULD BE DROPPED BY THE APP` if metadata is missing. A harness looser
   than production is a harness that certifies broken builds.

### Operational constraint worth knowing

**Repeated identical searches get throttled.** BLR→DPS stopped returning after
~8 runs of the same route and date within an hour, while BLR→DXB worked
immediately. The pipeline was fine; we had hammered one route. When testing, vary
the destination — and do not read a single timeout as a broken pipeline.

---

## The first end-to-end loop — gap analysis (2026-08-21)

Target: speak → context → auto-search flights → **agent reads the cheapest aloud**.
Widgets are deferred out of *this test only*, not dropped from the product.

### Decisions taken

| Question | Decision |
|---|---|
| Origin when unstated | **Default to Bangalore (BLR).** People rarely say where they fly from; waiting for it means the loop mostly never fires. |
| Search wait | **First site to land, ~6s** (`wait_for_all=False`, already supported). Cheapest-on-one-platform now beats cheapest-of-three too late. |
| What it says | **Just the result.** One sentence. `agent_status` toasts already cover acknowledgement, so voice stays rare. |
| Search date | today **+10 days**, when no date is spoken. |

### ⚠️ The latency budget is the design problem, not the code

| Stage | Time | Source |
|---|---|---|
| speech end → usable transcript | 1700ms | measured |
| classifier | 1050ms | measured |
| context extraction | ~1000ms | estimated (same shape as the classifier) |
| flight search | **16s** all sites / ~6s entry site | measured |
| TTS first byte | 600ms | measured |
| **sequential total** | **~20s / ~9s** | |

Twenty seconds is two conversational turns — they have moved on. Four levers,
highest payoff first:

1. **Prefetch on weak evidence.** `prefetch.warmed` already fires when the fast
   path hears a travel word. Starting the search there moves the whole 16s off
   the critical path. This is the single biggest win and the spec already
   mandates it.
2. **`wait_for_all=False`** — chosen above.
3. **Warm Chrome at worker boot**, not at first search. A cold profile is also
   what makes MMT time out.
4. **Run context extraction in parallel with the classifier**, not after — they
   are independent.

### What has to be built, per stage

- [x] **City name → IATA.** `agent/places.py`. The extension works purely in IATA
  codes (`lib/route.js` builds every URL from them and keeps only an
  Indian-airport set for `detectIntl`) and has **no name resolver**, so this is
  the one genuinely new piece. A curated table rather than asking the model:
  Claude knows Bali is DPS and will also confidently produce a plausible code for
  a city it is unsure about — and a wrong code does not fail, it searches the
  wrong city and the agent reads a real price for the wrong place aloud.
  Keyed to `vocabulary.py`, with a test asserting every keyterm has a code, so
  hearing and resolving cannot drift apart.
- [ ] **`TripContext` + extractor.** Not built. Own lagging cadence (§4.3), so it
  stays off the trigger path. Needs change-detection — re-searching on every
  utterance is both slow and expensive.
- [ ] **Search trigger.** Ready-predicate (origin + destination resolved), search
  once per route, date default = today + 10.
- [ ] **Search wiring.** `cross_search` is already async so it will not block the
  event loop, but `ensure_chrome` is a blocking subprocess launch and needs an
  executor. Must handle `partial`/`error`, not just `complete`.
- [ ] **Cheapest selection.** A `min` across sites, not a merge — that respects
  "keep data separate from different sites" while still answering the question.
- [ ] 🔴 **Fix the Ixigo timestamp bug first.** Ixigo emits `Z` (UTC), Cleartrip
  emits local+offset. This was a display bug when it was a widget; spoken, it
  becomes "departs 2:40am" for an 8:10am flight — and **a misheard time cannot be
  re-checked the way a card can.** Blocking for this loop.
- [ ] **Voice path.** Not built. Audio track published at join (silent when idle,
  so there is no negotiation delay at speak time), 150ms earcon to mask the
  600ms TTS first byte, streaming synthesis, and barge-in via
  `AudioSource.clear_queue()` — the source buffers ~1s ahead, so cancelling the
  push loop alone leaves a second of agent audio playing.

### Known non-blocking constraints

- **Chrome-on-this-laptop is a dev-only architecture.** Fine for this test; the
  agent worker and the browser being co-located will not survive deployment.
- **ElevenLabs free tier is ~10k characters ≈ 100 remarks.** Enough to prove the
  path, not enough to iterate on how it sounds.

---

## Phase 3 — Decision layer (weeks 5–6) 🔴 all remaining product risk

Nobody knows the right trigger rules. This phase is empirical; the replay harness is what makes it converge.

- [ ] State machine: `LISTENING → PROCESSING → RESPONDING → COOLDOWN(20s) → LISTENING`
- [x] **Fast path (§5.1a):** `agent/wake.py` — fuzzy wake-name matching on **interim** transcripts, plus a separate travel-keyword warm signal. Verified firing on live audio: `trigger.fired matched=copilot` on the first interim of "Hey, Copilot.", exactly once per phrase.
  - Interpretation recorded: the spec's "wake name **plus** a travel-keyword gate" is implemented as two signals, not a conjunction. A conjunction would mean a bare "hey copilot?" does not fire, and §5.1a says this path may only *add* a trigger. Name alone → direct address; travel words alone → warm the prefetch, never fire.
  - Matching handles what STT actually produces: "co pilot", "co-pilot", "copilots", "COPILOT". Rejects "cockpit", "pilot", "copy that". Edit-distance tolerance is length-scaled so short tokens match exactly.
  - Fires once per *speech segment*, per speaker, because interims are cumulative — a naive matcher fires once per interim and triggers four times for one phrase.
- [x] Publish `agent_status` on a fast-path hit — `agent/widgets.py`. Verified: both participants receive and decode it on a live call.
  - The envelope is duplicated across Python and Swift, and a mismatch produces a **blank screen on a phone** rather than an error anywhere. So `tests/test_widgets.py` reads `WidgetModels.swift` and `CallCenter.swift` directly and asserts the version guard, the topic string, the snake_case convention and the `agent_status` field names against the actual Swift source.
  - Broadcast, not targeted: a card only one party could see would confuse both.
  - A failed publish never raises — the humans are talking to each other and the agent is an accessory to that.
- [x] **Guardrails as a testable unit:** `agent/policy.py` — state machine, cooldown, separate widget/speech budgets, speech floor, hold-and-expire. 40 tests.
- [ ] **Semantic path (§5.1b)** — 🔒 blocked on `ANTHROPIC_API_KEY`: `claude-haiku-4-5` per finalized utterance, output schema-enforced via `output_config.format`. No `effort` param. 1.5s timeout → `none`.
- [ ] **Schema pre-warm at worker boot** (a cold schema compile would blow the 1.5s timeout on the first real utterance)
- [ ] **`TripContext` extraction unbundled** onto its own lagging cadence (§4.3)
- [x] Guardrails: 20s cooldown (direct address bypasses), max 1 proactive widget / 2 min, **separate 1-per-2-min proactive speech budget**, defer while both humans overlap, hold-and-expire
  - Recorded because it is easy to misread: after a proactive action the 120s budget dominates and **the 20s cooldown never binds**. The cooldown's real job is spacing a proactive action after a *direct* one.
  - `evaluate()` is a pure query; only `record_action()` spends budget. Folding them would consume a slot for a trigger that was evaluated and then dropped, silently refusing the next real opportunity.
- [ ] Replay-test harness with `tests/replays/*.txt` and expected outcomes

**Acceptance**
- [ ] Replay tests hit expected triggers with no audio and no network
- [ ] Wake word detected **< 200ms after the interim arrives** (our own processing budget). The end-to-end figure is that plus the measured ~500ms interim lag — see the Phase 2 latency note; the original "<200ms after the word is spoken" is not reachable through a hosted STT socket.
- [ ] Semantic trigger verdict **< 600ms** after end of utterance
- [ ] A classifier timeout degrades to `none` and the call continues
- [ ] Debug overlay shows the last verdict + confidence live on the call

---

## Phase 4 — Widgets + flight tool live (week 7)

> **Flight search is already done** (2026-08-21), ahead of sequence, because
> Kushal wanted it synced first. It runs through his **Watermelon** Chrome
> extension rather than a flight API:
>
> | | |
> |---|---|
> | Entry | navigate real Chrome to `buildSearchUrl('cleartrip', route)` |
> | Fan-out | the extension's own `search-detected` → `startCrossSearch`, offscreen iframes |
> | Read | poll its IndexedDB `crossSearches` store |
> | Platforms | Cleartrip + Ixigo + MMT (Skyscanner/IndiGo excluded by design) |
> | Timing | entry site ~2s · all three ~16s |
> | Code | `flight_scout/watermelon.py`, `flight_scout/cleartrip.py`, `flight_scout/capture.py` |
> | Zero changes | to the extension itself |
>
> Hard requirement: **a warm, persistent Chrome profile.** On a cold profile MMT
> times out and the cross-search never leaves `partial`. Data is kept **per-site**
> — `lib/analysis/dedupe.js` owns merging.
>
> Still to do here: `render_widget` tool, LiveKit data-message publish, per-call
> rate cap, and converting Ixigo's `Z` timestamps to origin-local before display.



- [ ] `render_widget` tool — schema-validated args; publish to the room the instant the tool call arrives
- [ ] LiveKit data messages, reliable, topic `"widget"`, versioned envelope, payloads **< 14KB**, URLs not images
- [ ] iOS `WidgetRenderer` switching on `type`; **unknown `type` → drop silently + log** (forward compatibility)
- [ ] Real flight API wired behind the §6 interface; response normalized to top 3 options in worker code
- [ ] Tool-use loop (`claude-sonnet-5`), hard cap 2 tool calls + 1 response, 10s budget
- [ ] **Speculative prefetch** on `TripContext` change, cached on `(origin, destination, date_range, pax)`, bounded per call, hit/miss logged
- [ ] Typed failure → `agent_status` error toast. **Never hallucinate flight results.**

**Acceptance**
- [ ] Card appears on both phones **< 500ms** after publish
- [ ] Say "flights to Bali second week of December from Bangalore" on a real call → real results card
- [ ] Prefetch cache hit rate logged and non-zero
- [ ] Flight API forced to fail → toast appears, no fabricated results, call unaffected

---

## Phase 5 — Voice (week 8)

- [ ] One shared agent audio track, published (silent) at join
- [ ] TTS streamed from the model's **text response** (not a JSON field), first chunk published not buffered
- [ ] **Earcon** ~150ms before speech
- [ ] **Hold-and-expire:** synthesize immediately, wait for a 700ms shared-silence window, **drop the voice after 5s** and let the widget carry it
- [ ] **Barge-in:** duck ~120ms → cut at word boundary within 200ms → else hard stop. No retry after barge-in; degrade to widget.
- [ ] Proactive triggers: discard generated text before TTS (code-enforced, not model-trusted)
- [ ] Log `tts.start`, `tts.first_byte`, `tts.cancelled`, and **barge-in rate**

**Acceptance**
- [ ] Direct address gets a spoken answer, p50 **≤ 1.6s** from end of utterance to first audio
- [ ] Speaking over the agent cuts it off **< 300ms**
- [ ] An answer with no available silence window is **dropped, not spoken late**
- [ ] Barge-in rate measured over ≥ 10 real calls → `____%` (if > 30%, fix trigger timing, not TTS)

---

## Phase 6 — Harden (week 9)

- [ ] Retention enforced: no raw audio anywhere; transcript + TripContext + widgets TTL'd at 30 days
- [ ] Every §10 failure mode provoked at least once in a test
- [ ] `SECURITY.md` — including: *no write-capable tool may be added without a security review*
- [ ] Contacts endpoint no longer returns "everyone" once past the seed list
- [ ] Cost + latency rollup per call from logs

---

## Phase 7 — Distribution (later, deliberately last)

- [ ] APNs + PushKit VoIP push replacing WebSocket ringing (pure transport swap; downstream path unchanged)
- [ ] `reportNewIncomingCall` called **synchronously** in the push handler (Apple terminates the app otherwise)
- [ ] Sign in with Apple behind `AUTH_MODE=apple`, issuing the identical session JWT
- [ ] Post-call recap screen from the persisted transcript

---

## Invariants — never break these

1. **The agent may fail; the call may not.** Every agent failure degrades to a silent, working phone call.
2. **Never mix the two audio tracks.** Speaker attribution comes from participant identity, for free, with zero error.
3. **The model never authors rendered content.** It picks tool arguments and selects results; widget fields come from the API.
4. **No raw audio is ever persisted.**
5. **No write-capable tool without a security review.** The transcript is untrusted input.
6. **Trigger latency is sacred.** Never trade it for token cost — cost is optimized elsewhere.
7. **Every pipeline stage ships with:** canonical events emitted, latency measured, a replay or fake-caller test, and its §10 failure mode provoked once.

## Open questions

- [ ] Wake name — "Copilot" is the placeholder. Something less generic and easier for Deepgram to catch reliably?
- [ ] `claude-sonnet-5` vs `claude-opus-5` + fast mode for the reasoning turn — A/B on real calls in Phase 5
- [ ] Post-call recap: Phase 7, or earlier as a cheap retention hook?
- [ ] Second widget type after `flight_results` (hotels? restaurants?) — decides how general the envelope needs to be

## Metrics worth a dashboard

| Metric | Why |
|---|---|
| Barge-in rate | Is voice socially working? |
| Trigger counts by type, and `trigger.suppressed` by reason | Are the rules right? |
| Prefetch cache hit rate | Is the biggest latency lever working? |
| p50/p95 per stage (`stt.final`, `classifier.result`, `llm.turn_end`, `tts.first_byte`) | Where the time actually goes |
| Cost per call | Sanity, not a constraint |
| Widget dismiss rate | Are proactive triggers welcome or annoying? |
