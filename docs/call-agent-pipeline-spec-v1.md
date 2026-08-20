# Spec: Two-Person Call + In-Call AI Agent Pipeline (v1)

**Status:** Ready for implementation handoff — **amended 2026-08-20** (search `AMENDMENT` for the changes). Live plan: `docs/EXECUTION_PLAN.md`.
**Scope:** This document covers ONE workflow end-to-end: User A calls User B → both connect → AI agent joins the room → agent continuously ingests both audio streams → agent decides when to act → agent responds via widget (data channel) and/or voice (TTS audio track). Flight search is the only tool in v1.
**Out of scope for this doc:** social graph / discovery, payments, Android, multi-party (3+ humans) calls.

---

## 0. Stack Decisions (locked for v1)

| Layer | Choice | Why | Swap path |
|---|---|---|---|
| Realtime transport | **LiveKit Cloud** | WebRTC SFU + agent framework + data channels in one primitive (the Room). No self-hosting in v1. | Self-host LiveKit OSS later; API-compatible. |
| iOS client | **SwiftUI + LiveKit Swift SDK** (`client-sdk-swift`) + CallKit + PushKit | Native call UX; SDK handles tracks/data. | — |
| Agent runtime | **LiveKit Agents (Python, 1.6.x)** — pin the exact version in `requirements.txt`; the API surface changed across 1.x releases, so do not copy pre-1.5 samples | Agent joins the room as a participant; audio/data plumbing is handled | Node SDK exists if team prefers TS |
| STT | **Deepgram streaming (Nova family, multilingual/code-switch model)** | Low-latency streaming, interim results, word timestamps, handles Hindi–English code-switching (important for our users) | AssemblyAI streaming |
| Reasoning LLM (agent turn) | **`claude-sonnet-5` via Anthropic API, tool use** — A/B against **`claude-opus-5` with fast mode** (`speed: "fast"`, beta `fast-mode-2026-02-01`, Claude API only, own rate-limit pool) | Strong tool use + instruction following. Fast mode runs full Opus at up to 2.5x output tok/s — the right trade for a latency-critical voice turn | Any tool-use LLM; isolated behind one module |
| Trigger classifier | **`claude-haiku-4-5`** with **structured outputs** (`output_config.format`) for schema-enforced JSON | Runs on every finalized utterance; must be cheap + fast. Note: `effort` is not supported on Haiku 4.5 — do not pass it. Prompt caching is unavailable on this path (4096-token minimum cacheable prefix on Haiku 4.5, far above our window) — do not build caching machinery for it | Local small model later |
| TTS | **ElevenLabs Flash v2.5 via LiveKit plugin** (confirmed 2026-08-21) | Quality + official LiveKit plugin; ~75ms model latency | Deepgram Aura-2 — already reachable on the STT key and already in the installed plugin, so it is a genuinely one-line fallback. Cartesia Sonic if both disappoint. |
| Control plane | **FastAPI (Python) + Postgres** | Small surface: auth, directory, call orchestration, token minting | — |
| Push | **APNs: PushKit (VoIP push) for incoming calls; standard APNs for everything else** | Required for real incoming-call UX on iOS | — |

**Explicit decision:** ElevenLabs is used ONLY for TTS. It is not the agent platform. The agent platform is LiveKit Agents; ElevenLabs is a plugin inside the agent's pipeline. (ElevenLabs has its own conversational-agent product — we are NOT using it, because it is built for human↔bot calls, not human↔human calls with an ambient third participant.)

**Critical architectural insight — no diarization needed:** In a LiveKit room each human publishes their own audio track. The agent subscribes to the two tracks *separately* and runs one STT stream per track. Speaker identity comes from track/participant identity, for free, with zero diarization error. Never mix the audio and diarize; that path is strictly worse.

---

## 1. Identities, Auth, and the Control Plane

### 1.1 Auth — DEV-LOGIN MODE is the v1 default (Apple Sign-In behind a feature flag)
We do not need Apple Sign-In (or TestFlight) to test with real people. v1 ships with a dev auth mode:

- **Seeding:** an admin script (`scripts/seed_users.py`) creates users on the backend: `{display_name, user_code}` where `user_code` is a short human-typeable code we hand out (e.g. `KUSHAL-7F3`, `ROHAN-2K9`).
- **Login screen (dev mode):** two fields — name + code. Client calls `POST /v1/auth/dev-login {user_code, display_name}`. If the code matches a seeded user, backend returns the same session JWT structure production auth will use. That request IS the login event.
- **Why this beats Apple for now:** deterministic test pairs, zero Apple account friction, works on any device/simulator instantly, and the rest of the system (JWT sessions, token minting) is identical to production — so swapping in Sign in with Apple later touches exactly one module.
- `AUTH_MODE=dev|apple` env flag on the backend; the iOS app reads a matching build config flag to show the right login UI. Sign in with Apple implementation is deferred; when built, it verifies Apple's identity token and issues the same session JWT.
- `users` table: `id (uuid)`, `auth_kind ('dev'|'apple')`, `user_code (nullable, unique)`, `apple_sub (nullable)`, `display_name`, `avatar_url`, `apns_device_token`, `voip_push_token`, `created_at`.
- Every API below requires `Authorization: Bearer <access_jwt>`. Dev-login is rate-limited (5 attempts/min/IP) and only enabled when `AUTH_MODE=dev`.

### 1.2 Control-plane endpoints (v1 complete list)

```
POST /v1/auth/dev-login           # body: { user_code, display_name } → session JWT (AUTH_MODE=dev only)
POST /v1/calls                    # caller initiates; body: { callee_id }
POST /v1/calls/{id}/accept        # callee accepts; returns LiveKit token
POST /v1/calls/{id}/decline
POST /v1/calls/{id}/end
GET  /v1/users/contacts           # who I can call (v1: everyone in a seeded list)
POST /v1/devices/voip-token       # register PushKit token
```

**AUTHORIZATION (do not skip).** `/accept`, `/decline`, and `/end` MUST verify the authenticated user is a participant on that `call_id` — `/accept` and `/decline` require `user.id == calls.callee_id`; `/end` allows either party. Without this check a `call_id` is a room key and anyone holding one can join the call. `POST /v1/calls` is rate-limited per caller AND per callee (the callee-side limit is what stops one user being used as a ring-spam target).

### 1.3 Call state machine (server-side, single source of truth)

```
INITIATED → RINGING → CONNECTING → ACTIVE → ENDED
                 ↘ DECLINED / TIMEOUT (45s ring timeout)
```

`calls` table: `id`, `room_name (uuid, opaque)`, `caller_id`, `callee_id`, `state`, `started_at`, `ended_at`.

**AMENDMENT (2026-08-20): no consent/permission layer in v1.** The agent always joins. There is no `/consent` endpoint, no `caller_consent`/`callee_consent` columns, no `AGENT_ACTIVE` state, and no mid-call kill switch. The agent is dispatched when the call reaches `ACTIVE`. Retained: the on-screen "agent is in the room" indicator (honesty about who is on the call, not a permission gate) and the no-raw-audio-persistence rule.

### 1.4 LiveKit access tokens
Minted server-side with the LiveKit server SDK. Never mint on device. Grants for a human participant:

```json
{
  "identity": "<user_uuid>",
  "name": "<display_name>",
  "ttl": "120s",                  // join window only; session persists after join
  "video": {
    "room": "<room_uuid>",
    "roomJoin": true,
    "canPublish": true,           // audio only enforced client-side in v1
    "canSubscribe": true,
    "canPublishData": false       // humans do NOT publish data in v1
  }
}
```

Agent token: same, plus `canPublishData: true`, identity `agent:trip-copilot`, and participant metadata `{"kind":"agent"}` so clients can distinguish it.

---

## 2. Call Establishment Flow (exact sequence)

```
Caller iOS                    Control Plane                 Callee iOS
   |  POST /v1/calls  ───────────►|
   |                              |  create LiveKit room (uuid)
   |                              |  insert calls row (INITIATED)
   |◄── 201 {call_id, lk_token} ──|
   | connect(LiveKit, token)      |── VoIP push (PushKit) ─────►|
   |                              |     payload: {call_id,      | reportNewIncomingCall
   |                              |      caller_name, room}     | (CallKit, MANDATORY, see 2.2)
   |                              |◄─ POST /calls/{id}/accept ──| (user taps Accept)
   |                              |── 200 {lk_token} ──────────►|
   |                              |                             | connect(LiveKit, token)
   |◄═══════════ WebRTC audio via LiveKit SFU ════════════════►|
   |                              |  both joined → state ACTIVE |
```

### 2.1 iOS project configuration (do not skip any)
- Capabilities: **Background Modes → Voice over IP, Audio**; **Push Notifications**.
- `Info.plist`: `NSMicrophoneUsageDescription`.
- Frameworks: `CallKit`, `PushKit`, `AVFAudio`, LiveKit SPM package.

### 2.2 PushKit + CallKit rules (Apple will kill the app if violated)
- On `pushRegistry(_:didReceiveIncomingPushWith:)` you MUST call `CXProvider.reportNewIncomingCall` **synchronously before the method returns**. Failure = app termination and eventual revocation of VoIP push.
- Audio session: do NOT call `AVAudioSession.setActive(true)` yourself when using CallKit. Configure category in `CXProviderDelegate.provider(_:didActivate:)`:
  - category `.playAndRecord`, mode `.voiceChat`, options `[.allowBluetooth, .defaultToSpeaker]` (speaker default because users will look at widgets on screen).
- Order of operations on accept: `CXAnswerCallAction` → fetch LiveKit token (`/accept`) → `room.connect()` → publish local mic track → fulfill the action.
- Outgoing calls also go through CallKit (`CXStartCallAction`) so the system UI, interruption handling (incoming cellular call), and audio routing behave.

### 2.2b Dev-mode testing WITHOUT TestFlight or APNs (build this first)
- Debug builds install directly from Xcode onto two physical iPhones (free with any dev account) — TestFlight is not required to test with two people. One device + one iOS Simulator also works for audio (Simulator uses the Mac's mic/speaker).
- **PushKit/VoIP push does NOT work on the Simulator and requires APNs setup even on device.** So dev builds get an incoming-call fallback: when `AUTH_MODE=dev`, the app (while foregrounded) opens a WebSocket to the control plane (`GET /v1/events` upgrade) and receives `{type:"incoming_call", call_id, caller_name}` events; on receipt it still reports to CallKit exactly as it would from a push, so the downstream code path is identical. Sequence: implement WebSocket ringing first → entire product is testable end-to-end → wire PushKit in a later step purely as a transport swap for background/locked-phone ringing.
- Practical dev loop: seed 2 users → log in on 2 devices with codes → call, talk, watch agent logs stream (§12).

### 2.3 LiveKit client connection (both sides)

```swift
let room = Room()
try await room.connect(url: LIVEKIT_WS_URL, token: token,
                       connectOptions: ConnectOptions(autoSubscribe: true))
try await room.localParticipant.setMicrophone(enabled: true)
// audio publish options: Opus, mono, ~32–48 kbps is plenty for speech
```

- Handle `RoomDelegate` events: participant joined/left, connection quality, `didReceiveData` (widgets, §7), reconnecting/reconnected (LiveKit auto-reconnects; show a "reconnecting" pill, do not tear down the call for <10s blips).
- Call ends when: either user hits end (client → `/end` → server deletes room via server API, which disconnects everyone), or room empties.

---

## 3. Agent Dispatch (when and how the agent enters)

- The agent is a **LiveKit Agents worker** process (Python) deployed as a long-running service registered against our LiveKit project. Use **explicit dispatch**, not automatic room-join: the control plane calls LiveKit's AgentDispatch API for room X when the call transitions to `ACTIVE` (both humans joined).
- **No consent gate (amended 2026-08-20).** The agent joins every call. A persistent on-screen indicator shows whenever the agent participant is in the room — drive it from the participant list + metadata `kind=agent`, not from local state. The indicator is not dismissible and is not a toggle.
- Agent identity/permissions per §1.4. The agent must survive human reconnects (it stays in the room as long as the room lives) and must exit + flush state when the room closes.

---

## 4. Audio Ingestion Pipeline (inside the agent worker)

**Important implementation note for the coding agent:** LiveKit Agents' default `AgentSession` behavior is conversational — it assumes one human, auto-detects turns, and replies after each user turn. **We must NOT use that default loop.** Our agent is *ambient*: it listens to two humans and only acts when our own decision layer (§5) says so. Concretely:

- Do not rely on the session's built-in "reply after turn" behavior. Subscribe to both remote audio tracks manually in the job entrypoint and attach one streaming STT instance per track. Disable/ignore automatic agent replies; all output is initiated by our state machine.
- Keep the framework's audio plumbing (track → PCM frames) and its TTS/audio-publishing helpers; discard its conversational turn loop.

### 4.1 Per-track flow

```
Remote track (Opus 48kHz) ─decode→ PCM frames (10ms)
  ─resample→ 16kHz mono PCM16
  ─websocket→ Deepgram streaming session (one per participant)
       ├─ interim transcripts ──► FAST PATH: local wake-word + keyword gate (§5.1a)
       ├─ FINAL transcripts {text, words[], start, end, confidence}
       └─ endpointing events (speech_final)
```

**AMENDMENT (2026-08-20): interim transcripts are NOT discarded.** They feed the fast trigger path in §5.1a. Waiting for a FINAL transcript costs ~300ms of endpointing before the agent can even know it was addressed; matching the wake name on an interim result detects direct address roughly **150ms** after the word is spoken, mid-sentence, with no model call and no network hop. Interim results remain unsuitable for the semantic classifier and for the transcript log — only the fast path consumes them.

- Two independent Deepgram sessions, tagged with `participant_identity`. Configure: interim results ON, smart formatting ON, endpointing ~300ms, keep-alive pings every 5s during silence (Deepgram closes idle sockets).
- **Reconnect policy:** if an STT socket drops, buffer PCM in a ring buffer (max 15s), reopen socket, replay buffer. If reconnect fails 3× in 30s, mark that speaker's transcription degraded and surface a debug metric — the CALL MUST CONTINUE (humans are unaffected; agent just misses context).

### 4.2 Transcript aggregator (single component, unit-test heavily)
Merges the two per-speaker streams into one ordered, **speaker-attributed** conversation log. Key property: attribution is never inferred — each Deepgram session is bound to exactly one participant's track, so every fragment is born tagged with `speaker_id = participant_identity`. The aggregator's only real job is ordering.

**Timestamp mechanics (one shared clock):**
1. When the agent opens an STT session for a track, record `stream_epoch = monotonic_now()` at the first PCM frame sent.
2. Deepgram returns word/utterance timings as offsets relative to the audio stream start. Absolute time: `t_start = stream_epoch + dg_start_offset`, `t_end = stream_epoch + dg_end_offset`.
3. Both tracks are processed in the same agent process, so both `stream_epoch`s come from the same monotonic clock — timestamps are directly comparable across speakers. (If an STT session reconnects, set a new `stream_epoch` for the replacement stream; ring-buffer replay offsets are accounted for by subtracting buffered duration.)

**Merge algorithm:** on every FINAL utterance, construct
`{utterance_id (call-scoped increasing int), speaker_id, speaker_name, text, t_start, t_end, confidence}`
and insert into the call log sorted by `t_start` (insertion near the tail; a simple bisect insert is fine). Overlapping speech is expected and preserved as overlapping intervals — never merge, truncate, or serialize overlaps; the label makes them unambiguous.

**How the AI sees it — every text sent to any model is speaker-labeled.** The aggregator exposes `render_window()` which formats the rolling window as a script; raw unlabeled text never reaches a model:

```
[00:38] Rohan: december is packed at work man
[00:41] Kushal: what about second week of December instead
[00:44] Rohan: yeah that works but keep it under 30k each
[00:44] Kushal: (overlapping) and direct flights only
```

Format rules: `[mm:ss]` from call start; `speaker_name` from the participant's display name; `(overlapping)` annotation when an utterance's interval intersects the previous one; utterances with confidence < 0.5 rendered with a `(unclear)` marker so the model can discount them. This exact rendering feeds both the Haiku classifier and the Sonnet tool turn — one implementation, golden-tested.

**State maintained:** (a) full call log (in memory), (b) **rolling window** of the last ~120 seconds / 40 utterances that feeds the decision layer, (c) `TripContext` object (below).

**Unit tests required (no audio, pure data):** interleaved ordering across two streams; heavy overlap; reconnect with replay; low-confidence rendering; window eviction; deterministic `render_window()` snapshots.

### 4.3 TripContext (the "kept context")
Updated by a cheap extraction step running on its own cadence.

**AMENDMENT (2026-08-20): context extraction is unbundled from the trigger classifier.** The original design bundled both into one Haiku call per utterance "so we pay for one cheap call per utterance" — trading latency for a cost saving we do not need (a 10-minute call is roughly $0.18 in classifier tokens). Split them so each is tuned for what it actually needs:

| Job | Cadence | Latency requirement |
|---|---|---|
| Trigger detection (§5.1) | Every finalized utterance | Hard — sits directly in the response path |
| Context extraction (this section) | Its own cadence; may lag several seconds | None — nobody perceives a destination list updating late |

The extraction call may batch several utterances and may run behind the live conversation. The trigger check must not.

```json
{
  "origin_candidates": ["BLR"],
  "destination_candidates": ["Bali", "Da Nang"],
  "date_signals": [{"text": "second week of December", "resolved_range": ["2026-12-07","2026-12-13"], "confidence": 0.7}],
  "party_size": 2,
  "budget_signals": [{"text": "under 30k each", "amount": 30000, "currency": "INR"}],
  "last_updated_utterance": 141
}
```

Rules: append/merge, never destructively overwrite (people change their minds — keep candidates ranked by recency). This object, not raw transcript, is what the tool-calling turn receives, plus the rolling window.

---

## 5. Decision Layer (when the agent acts)

State machine (per call):

```
LISTENING ──(trigger fires)──► PROCESSING ──► RESPONDING ──► COOLDOWN(20s) ──► LISTENING
```

### 5.1 Triggers — two-speed (amended 2026-08-20)

The trigger layer runs at two speeds. The fast path exists so direct address does not wait on endpointing plus a model round-trip.

#### 5.1a Fast path — local, on interim transcripts (~150ms)
Pure local matching on the interim transcript stream (§4.1): the configurable wake name (fuzzy — "copilot", "co-pilot", "co pilot") plus a travel-vocabulary keyword gate. No network, no model.

- A wake-word match **fires the trigger immediately**, mid-utterance, and publishes an `agent_status` widget at once so the humans see the agent engaging while it works.
- **This path may only ADD a trigger, never suppress one.** Absence of a keyword match is not evidence of no intent — that judgement belongs to §5.1b.
- On a fast-path hit, start warming the reasoning turn on the partial utterance and revise when the FINAL arrives.

#### 5.1b Semantic path — Haiku classifier, on every FINAL utterance (~550ms end-to-end)
Runs per finalized utterance; catches intent carrying no wake word.

1. **Direct address** — classifier confirms the agent is addressed (handles code-switching and phrasings the regex misses). → fires, allowed VOICE + WIDGET.
2. **High-confidence flight intent** — classifier returns `flight_intent` with confidence ≥ 0.85 AND `TripContext` has at least destination + coarse dates. → fires, **WIDGET-ONLY** (silent push; two humans mid-conversation are not interrupted audibly).
3. **Widget tap** (user taps "ask copilot" on a rendered widget → client sends data message → treated like direct address).

Classifier call: `claude-haiku-4-5`, input = rolling window, output schema-enforced via `output_config.format` — `{intent: none|flight_intent|direct_address, confidence}`. It no longer returns `context_updates` (see §4.3). Timeout 1.5s; on timeout → treat as `none`. Do not pass `effort` (unsupported on Haiku 4.5).

**AMENDMENT (2026-08-21): the classifier timeout is 2.5s, not 1.5s, and the
output schema must not use numeric bounds.**

Both from measurement against the live API from a worker in India South:

- **Latency is p50 1050ms, max 1400ms, cold first call 2041ms.** 1.5s leaves
  ~100ms of headroom over the observed maximum, and a live call duly timed out at
  1502ms — which silently dropped the entire trigger, because a timeout is
  indistinguishable from `none`. 2.5s does not cost anything real: the 1700ms
  finalize lag (§4.1 amendment) dominates the path either way.
- **Prompt caching does not apply here.** The system prompt is ~380 tokens and
  Haiku's minimum cacheable prefix is 2048, so `cache_creation` and `cache_read`
  are both zero. Cost is ~$0.00067/utterance, ~$0.081 per ten-minute call.
- **`minimum`/`maximum` are rejected on `number` types** by the structured-output
  validator (400: "For 'number' type, properties maximum, minimum are not
  supported"). `confidence` is bounded by clamping in code instead.
- **Pre-warm is mandatory, not advisory.** The 2041ms cold first call exceeds even
  the raised timeout, so without a throwaway call at worker boot the first
  utterance of the first call is always missed.

**Corollary for §5.1a: the fast path must be able to drive the work, not only the
acknowledgement.** Both single-path failures have now been observed live — STT
transcribed "Hey copilot" as "Alert." and only the classifier caught it; then the
classifier timed out and only the wake word caught it. Either path alone drops
roughly one request in three, so a fast-path hit starts the search directly and
the classifier's verdict is a parallel route to the same action, never a gate on
it. This is the concrete form of "may only ADD a trigger, never suppress one".

**Schema pre-warm (required):** a new JSON schema incurs a one-time compilation cost on first use, cached 24h. Fire one throwaway classifier call at worker boot so the first real utterance of the first call does not eat it and blow the 1.5s timeout.

**Debounce is explicitly rejected for the trigger path.** Classifying every Nth utterance to save tokens delays the "the agent wants to speak" moment by seconds — the one thing this product cannot afford. Classify every finalized utterance.

### 5.2 Guardrails
- COOLDOWN 20s after any agent action (direct address bypasses cooldown).
- Max 1 proactive (non-addressed) widget per 2 minutes.
- If both humans are actively overlapping/speaking, defer RESPONDING until 700ms of shared silence (don't talk over people); widgets can render anytime.
- **Barge-in:** while the agent is speaking, if either human starts speaking (VAD on either track), cancel TTS playback within 200ms and return to LISTENING.

### 5.3 PROCESSING (the expensive turn)
Tool-use loop, hard cap 2 tool calls + 1 final response, 10s total budget:
- System prompt: role, response contract (below), safety rules (only discuss travel; never repeat sensitive personal content from the call; refuse anything that isn't the flight task).
- Input: `TripContext` + rolling window + trigger type.
- Tools: `search_flights` (§6), `render_widget` (below).

**AMENDMENT (2026-08-20): the output contract is a tool call plus streamed text, not a JSON blob.**

The original contract was strict JSON `{speak, widget}`. That blocks streaming — TTS cannot start from a JSON string field until the whole object is generated and parsed, so the first audio byte waits on the last output token. Replace it with:

| Channel | Mechanism | Notes |
|---|---|---|
| Widget | The model calls a **`render_widget` tool** | Schema-validated in code. Published to the room the instant the tool call arrives — before the model finishes writing what it wants to say, so the card lands ahead of the voice. |
| Speech | The model's **final plain-text response** | Streams natively token-by-token, so TTS starts on the first tokens. ≤ 2 sentences. |

**The two channels are fully independent.** All four combinations are valid and expected: widget only + silent (the default for proactive triggers), speech only, both, or neither (the agent decided not to act).

Enforcement is unchanged in effect, only in layer: **if the trigger was proactive, discard any generated text before it reaches TTS.** Same guarantee as the original "`speak` MUST be null, validated in code, not trusted from the model."

**AMENDMENT (2026-08-21): proactive triggers MAY speak. Product decision — the
agent announces the result as it renders.**

This reverses the paragraph directly above, which is now void: proactive
generated text is no longer discarded before TTS. All four channel combinations
remain valid, but the default for a proactive trigger is now **widget + speech
together**, not widget-only-and-silent.

Naming what this costs, because it removes the one guardrail that made proactive
action unconditionally safe. Under the old rule a proactive misfire was a card
someone could ignore. Now a misfire interrupts a human conversation to say
something possibly wrong out loud — and the worst case is a wrong inferred
`TripContext` (§4.3) announced with confidence. Voice cannot be un-said; a widget
can be ignored.

So three mechanisms that were secondary are now **load-bearing, and none of them
is optional**:

1. **Rate limit — max 1 proactive speech per 2 minutes.** Previously a politeness
   cap on widgets. It is now the only hard bound on how often the agent can
   interrupt. Widgets keep their own, separate, more generous budget: rendering is
   cheap, speaking is not.
2. **Hold-and-expire (§8) is mandatory for proactive speech.** Synthesize into a
   buffer, wait for a socially valid window (700ms of shared silence), and **drop
   the voice entirely after 5s** while still rendering the widget. An unprompted
   remark has no right to seize the floor; a direct answer does.
3. **`TripContext` confidence gates speech, not the widget.** Below the
   confidence threshold, render the card and stay silent. This is the asymmetry
   that keeps the decision safe: the low-cost channel tolerates uncertainty, the
   high-cost channel does not.

**Direct address is unaffected** — it already spoke, bypasses cooldown, and skips
the confidence gate, because someone asked.

**Barge-in rate remains the metric that decides whether this was right (§8).** If
humans talk over proactive speech in more than ~30% of attempts, the answer is to
put the discard rule back, not to tune the voice.

The model never authors rendered content: `render_widget` arguments are schema-validated, and every field of `flight_results` originates from the flight API response. The streamed text is the only free-form model output reaching a human, and it is length- and topic-gated before TTS.

**Prompt-injection stance (v1):** transcript is untrusted input. The only tool is read-only flight search, so blast radius is a bad search. Still: tool arguments are schema-validated in code; the model cannot pass free-form strings into URLs; `speak` output is checked against a denylist of meta-instructions before TTS. No write-capable tools may be added without a security review — put this sentence in the repo README.

---

## 6. Flight Search Tool

Wrapper around the flight API (endpoint to be provided). The coding agent should build to this internal interface and stub it:

```python
async def search_flights(origin: IATA, destination: str, depart_range: DateRange,
                          return_range: DateRange | None, pax: int) -> FlightResults
# - resolve destination string → airport/city code via the API's own place-resolution
#   endpoint if available; else a static lookup table for top-200 cities in v1
# - timeout 6s; one retry; on failure return typed error (agent says/shows "couldn't fetch")
# - normalize response to WidgetPayload.flight_results (below) — the LLM never sees
#   raw API JSON larger than ~2k tokens; worker code does the trimming to top 3 options
```

**Speculative prefetch (added 2026-08-20).** Flight APIs routinely take 3–8s; a 6s timeout plus one retry is a 12s worst case, and an `agent_status` toast only labels the wait rather than removing it. `TripContext` already holds ranked destination and date candidates *before* any trigger fires — so warm the search on TripContext change, cached on `(origin, destination, date_range, pax)`. A trigger that hits a warm cache renders in ~50ms instead of ~4s. Bound it: at most one prefetch per newly-confident destination, a hard cap per call, and log every prefetch hit/miss so the hit rate is measurable. This is the single largest perceived-latency lever in the product.

---

## 7. Widget Channel (agent → phones)

- Transport: LiveKit **data messages**, reliable delivery, topic `"widget"`, published by the agent to the room (both participants receive identical payloads in v1).
- Size limit: keep payloads **< 14KB** (data channel practical limit ~15KB). No images inline — URLs only.
- Versioned envelope; clients must ignore unknown `type` (forward compatibility):

```json
{
  "v": 1,
  "widget_id": "uuid",
  "type": "flight_results",
  "ttl_s": 300,
  "payload": {
    "route": {"origin": "BLR", "destination": "DPS"},
    "date_range": ["2026-12-07", "2026-12-13"],
    "options": [
      {"carrier": "IndiGo", "flight": "6E 27", "depart": "2026-12-07T08:10+05:30",
       "arrive": "2026-12-07T16:35+08:00", "stops": 1, "duration_min": 390,
       "price": {"amount": 24350, "currency": "INR"}, "deeplink": "https://..."}
    ]
  }
}
```

- iOS: `RoomDelegate.didReceiveData` → decode envelope → `WidgetRenderer` (a SwiftUI view that switches on `type`) → animate card over the call surface (bottom sheet, dismissible, auto-expires after `ttl_s`). Unknown `type` → drop silently + log.
- v1 widget types: `flight_results`, `agent_status` (thinking/error toast). (`consent_state` removed — no consent layer, see §1.3.)

---

## 8. Voice Response Path

- TTS: ElevenLabs low-latency model via the LiveKit Agents ElevenLabs plugin, streamed — first audio chunk should be published, not buffered-then-played. Fed from the model's streaming text response (§5.3), so synthesis begins on the first tokens.
- The agent publishes **one** audio track at join (muted/silent when idle) so there is no publish-negotiation latency at speak time. Both humans hear the agent — it is a participant in a shared conversation, and a voice only one party could hear would be disorienting for both.
- Playback mixing is automatic (SFU forwards the agent track to both humans like any participant).

**AMENDMENT (2026-08-20): three additions to the voice path.** Because the agent shares one track, it must win the floor from two people — these are what make that socially acceptable.

**1. Earcon first.** Play a ~150ms non-speech tone before the words. Attention orients before content starts, so listeners do not miss the first few words and mentally replay them — and it buys ~150ms of TTS buffer, covering most of first-chunk latency.

**2. Hold-and-expire — voice has an expiry.** Decouple generating from saying. Synthesize into a buffer as soon as PROCESSING completes, then wait for a socially valid window (per §5.2: 700ms of shared silence). If no window opens within **5s, drop the voice entirely** and let the widget carry the answer. Never speak a stale answer — the widget already conveyed it, so the fallback loses nothing, whereas answering the question from eight seconds ago is worse than silence.

**3. Barge-in: duck, then cut at a word boundary.** Hard-cutting mid-syllable sounds broken. On VAD detecting either human: duck the agent track over ~120ms, stop at the next word boundary if one falls within 200ms, hard-stop otherwise. After a barge-in, **do not retry the speech** — degrade to widget.

**Barge-in rate is the primary voice health metric.** If humans interrupt the agent in more than ~30% of speak attempts, the trigger timing is wrong, not the TTS. Nothing else measures whether voice is socially working.

**Latency budget (end of human utterance → first agent audio), target p50 ≤ 1.6s:**

| Stage | Budget |
|---|---|
| STT finalization (endpointing) | 300ms |
| Classifier (only on direct address path this counts) | 250ms |
| LLM first tokens (no tool) / with 1 tool call | 500ms / +1.5–3s |
| TTS first audio | 250ms |
| Network/WebRTC | 150ms |

When a tool call is needed, the agent should immediately push an `agent_status: "searching flights…"` widget so the wait reads as work, not lag.

---

## 9. Data, Privacy, Retention (implement, don't defer)

- Raw audio: never persisted anywhere in v1. No recordings. Unchanged and non-negotiable.
- **Transcripts ARE persisted (amended 2026-08-20).** The original spec discarded them at room close. With no consent layer, persisting the speaker-attributed transcript costs almost nothing and buys three things: a post-call recap surface ("here's what you two decided"), a replay fixture for every real call at zero marginal effort (§11.3), and the ability to debug a call a day later instead of only while logs are hot. Keyed to `call_id`, same **30-day TTL** as the other artifacts.
- Persisted artifacts: full speaker-attributed transcript + `TripContext` final snapshot + widget payloads + call metadata, keyed to call id, 30-day TTL.
- Vendor config: Deepgram / Anthropic / ElevenLabs accounts must have zero-retention / no-training settings enabled where offered; record the settings in the repo's `SECURITY.md`.
- All secrets via environment; nothing in the iOS bundle except the LiveKit WS URL.

```
ENV (agent worker): LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET,
DEEPGRAM_API_KEY, ANTHROPIC_API_KEY, ELEVENLABS_API_KEY, FLIGHTS_API_KEY
ENV (control plane): DATABASE_URL, LIVEKIT_* , APNS_KEY_ID/TEAM_ID/P8, JWT_SIGNING_KEY
```

---

## 10. Failure Matrix (agent must degrade, call must not)

| Failure | Behavior |
|---|---|
| Agent worker crash | LiveKit removes participant; humans keep talking; control plane re-dispatches once; indicator updates |
| STT socket drop | Ring-buffer replay per §4.1 |
| LLM timeout | `agent_status` error toast; return to LISTENING |
| Flight API down | Typed error → toast "couldn't fetch flights"; never hallucinate results |
| Human network blip | LiveKit auto-reconnect; agent keeps per-identity STT session keyed by identity, re-attaches on track re-subscribe |
| Token expiry pre-join | Client refetches from control plane |

Observability details: see §11.

---

## 11. Logging, Testing & Debug Infrastructure (build alongside, not after)

Goal: when something misfires on a call, Kushal can point at the exact stage that got it wrong within minutes, from logs alone.

### 11.1 Structured logging (all services)
- Every log line is one JSON object: `{ts, level, service, event, call_id, utterance_id?, speaker_id?, latency_ms?, ...fields}`. `call_id` is minted by the control plane and propagated everywhere (LiveKit room metadata carries it so the agent worker picks it up at job start). `utterance_id` correlates a single utterance across its whole journey.
- **Canonical event names** (grep-able, exhaustive for the pipeline):
  `call.created, call.ringing, call.active, call.ended, agent.dispatched, agent.joined, agent.removed, track.subscribed, stt.session_open, stt.interim, stt.final, stt.reconnect, aggregator.utterance, classifier.request, classifier.result, trigger.fired, trigger.suppressed (with reason: cooldown|overlap|rate_limit), llm.turn_start, llm.tool_call, llm.turn_end, widget.published, widget.rendered (client-side), tts.start, tts.first_byte, tts.cancelled (barge-in), error.*`
- Every stage event carries its own `latency_ms`; `llm.turn_end` carries token counts and cost. This yields per-stage latency histograms, trigger counts by type, barge-in counts, and per-call cost rollups with no extra instrumentation.
- **Privacy split:** `level=info` logs metadata only (no utterance text). `level=debug` (non-prod only, `LOG_TRANSCRIPTS=true`) includes text — this is what you'll use during development. Prod defaults text OFF.
- Dev sink: pretty-printed console + a JSONL file per call (`logs/{call_id}.jsonl`). A tiny `scripts/call_report.py {call_id}` renders a human-readable timeline of a call: transcript interleaved with every classifier verdict, trigger, tool call, and widget — the primary debugging artifact.

### 11.2 iOS debug overlay (dev builds only)
A collapsible panel on the call screen showing: agent state (LISTENING/PROCESSING/RESPONDING/COOLDOWN — broadcast by the agent via `agent_status` data messages), last 5 transcript lines with speaker labels, last classifier verdict + confidence, last widget payload (raw JSON viewer), and connection quality. This is how you'll see "the classifier said none at 0.62 — that's why no widget appeared" live on the call.

### 11.3 Test harnesses (three layers, cheapest first)
1. **Transcript replay (no audio, no network)** — the decision layer (classifier prompts, state machine, cooldowns, TripContext extraction) is driven by a plain-text fixture format identical to `render_window()` output. `pytest` fixtures live in `tests/replays/*.txt` with expected outcomes (`expect: trigger=flight_intent at utterance 14`). Most behavior bugs get reproduced and fixed here. Add a replay file for every real-call misfire you find — this is the regression suite.
   **`call_report.py` MUST auto-emit a replay fixture for every real call** (added 2026-08-20). Every conversation then becomes a free regression-test candidate, and trigger tuning becomes a minutes-long loop instead of a days-long one. Since trigger rules are the only genuinely unknown part of this product, this is the highest-leverage piece of test infrastructure in the build.
2. **Fake caller (audio, no humans needed)** — `scripts/fake_caller.py --room X --wav fixture.wav --identity rohan` joins a room as a synthetic participant and publishes a WAV file as its mic. Two fake callers + the agent = full pipeline test (STT, aggregation, ordering, triggers, widgets) run by one person or in CI. Record a handful of scripted two-person conversations as WAV fixtures once.
3. **Two-device live testing** — dev-login (§1.1) + WebSocket ringing (§2.2b) on two phones; use `call_report.py` afterwards to audit what the agent saw and decided.

### 11.4 Acceptance rule for the coding agent
No pipeline stage is "done" without: its canonical events emitted, its latency measured, a replay or fake-caller test covering it, and its failure mode from §10 provoked at least once in a test.

---

## 12. Build Order for the Coding Agent (each step has a runnable acceptance test)

1. **Control plane skeleton** — dev-login auth (§1.1), `/calls` lifecycle, LiveKit token minting, WebSocket ringing (§2.2b), structured logging from line one (§11.1). ✅ Two seeded users log in with codes; two tokens minted; room visible in LiveKit dashboard; `logs/{call_id}.jsonl` created.
2. **iOS call MVP** — CallKit + LiveKit connect, WebSocket-driven incoming call (PushKit deferred), dev debug overlay shell (§11.2). ✅ Two devices (or device + simulator): call rings, audio both ways, survives 5s network drop. No APNs required.
3. **Agent joins, ears only** — worker with per-track Deepgram, aggregator with speaker-labeled ordered log (§4.2), fake-caller script (§11.3). ✅ Two fake callers playing WAV fixtures produce a correctly attributed, correctly ordered transcript with overlaps preserved; same verified on a live 2-phone call via `call_report.py`.
4. **Decision layer + TripContext** — Haiku classifier loop, state machine, cooldowns. ✅ Scripted transcript replay tests (pure unit tests, no audio) hit expected triggers.
5. **Widget channel** — agent publishes `flight_results` with mock data; SwiftUI renders card in-call. ✅ Card appears on both phones < 500ms after publish.
6. **Flight tool live** — real API wired, Sonnet tool loop, schema validation. ✅ Say "flights to Bali second week of December from Bangalore" → real results card.
7. **Voice path** — ElevenLabs streaming, pre-published track, barge-in. ✅ Direct address gets spoken answer; speaking over the agent cuts it off < 300ms.
8. **Retention** — §9 enforced. ✅ No raw audio anywhere; transcript persisted and TTL'd.
9. **PushKit/APNs + Apple Sign-In** — swap WebSocket ringing for VoIP push (background/locked ringing), enable `AUTH_MODE=apple`. ✅ Locked phone rings with native call UI; Apple login issues the same session JWT; nothing downstream changes.

**SUPERSEDED (2026-08-20) by `docs/EXECUTION_PLAN.md`**, which is the live checkable plan and the source of truth for sequencing. Material changes there: the consent step is deleted, a widget-feel spike is inserted early (it answers the only aesthetic question in the product and takes two days), and every phase carries explicit acceptance criteria.
