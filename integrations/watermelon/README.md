# Watermelon ↔ hands-free bridge

Lets the in-call agent run a cross-platform flight search using **your real
Chrome** — real cookies, real session, real fingerprint. That is the whole point:
the extension gets past the aggregators' anti-bot defences *because* it runs
inside a genuine browser doing genuine-looking things. A headless port would
recreate the parsing and throw away the reason it works.

```
agent worker            flight bridge            Chrome + Watermelon
------------            -------------            -------------------
search_flights()  ──▶   POST /v1/search
                        (holds the request)  ◀── GET /v1/jobs/next   (long-poll)
                                                 opens the Cleartrip
                                                 results URL in a
                                                 minimized window
                                                     │
                                                 existing search-detected
                                                 → startCrossSearch fans
                                                 out to MMT / Ixigo /
                                                 Skyscanner, exactly as
                                                 it does for a human
                                                     │
                        POST /jobs/{id}/result  ◀────┘
   normalized       ◀── merge + cache
   flight_results
```

**No new scraping.** The bridge adds a queue and a normalizer. Capture, parsing,
cross-search fan-out, dedupe and ranking are all the extension's existing code.

**IndiGo is not used** (dropped from scope 2026-08-21). `PLATFORMS` in
`flight_bridge/normalize.py` covers Cleartrip, MMT, Ixigo, Skyscanner.

---

## Why Cleartrip is the entry site

The extension normally triggers when a *human* lands on a search-results page.
For an agent-initiated search there is no human, so something has to be the entry
point — and Cleartrip's results URL is directly navigable:

```
https://www.cleartrip.com/flights/results?adults=1&...&depart_date=07/12/2026&from=BLR&intl=n&to=DEL
```

`lib/route.js`'s `buildSearchUrl('cleartrip', route)` already produces exactly
that. Open it in a background tab and the existing `search-detected` →
`startCrossSearch` path does the rest. Nothing else needed a change.

One subtlety the bridge deliberately defers to `route.js`: the **`intl` flag**.
Cleartrip returns `400 SEARCH_VALIDATION_FAILED: Invalid sector type flag` when
it disagrees with the route, so the bridge never sets it — `detectIntl(from, to)`
owns that decision.

---

## Installing

### 1. Copy the module

```sh
cp integrations/watermelon/handsFreeBridge.js <extension>/lib/bridge/handsFreeBridge.js
```

### 2. `manifest.json` — allow the loopback bridge

Add to `host_permissions`:

```json
"http://127.0.0.1:8787/*"
```

Loopback only. Never a LAN or public address: this endpoint can make your
browser navigate.

### 3. `background.js` — start the loop

Near the top, alongside the other imports:

```js
import { startBridgeLoop, stopBridgeLoop, bridgeStatus, setBridgeConfig }
  from './lib/bridge/handsFreeBridge.js';

startBridgeLoop().catch(() => {});   // no-op until enabled in storage
```

And add message handlers next to `force-cross-search` so the popup can control it:

```js
case 'handsfree-status':   sendResponse(await bridgeStatus()); break;
case 'handsfree-config':   sendResponse(await setBridgeConfig(msg.patch || {})); break;
case 'handsfree-start':    sendResponse(await startBridgeLoop()); break;
case 'handsfree-stop':     sendResponse(stopBridgeLoop()); break;
```

### 4. Enable it, with the bridge's token

Start the bridge and copy the token it prints:

```sh
.venv/bin/uvicorn flight_bridge.main:app --host 127.0.0.1 --port 8787
# [bridge] listening. BRIDGE_TOKEN=xxxxxxxxxxxx
```

Then, from the extension's service-worker console:

```js
chrome.runtime.sendMessage({
  type: 'handsfree-config',
  patch: { enabled: true, token: 'xxxxxxxxxxxx', baseUrl: 'http://127.0.0.1:8787' }
});
chrome.runtime.sendMessage({ type: 'handsfree-start' });
```

Pin the token with `BRIDGE_TOKEN=...` in the environment so it survives restarts.

---

## Verifying

```sh
curl -s localhost:8787/healthz | python3 -m json.tool
```

`browser_connected: true` means the extension is polling. Then:

```sh
curl -s -X POST localhost:8787/v1/search \
  -H "X-Bridge-Token: $BRIDGE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"origin":"BLR","destination":"DEL","depart_date":"2026-12-07"}' \
  | python3 -m json.tool
```

A minimized window should appear and disappear, and you should get back three
cheapest-first itineraries with a per-platform price breakdown.

---

## Behaviour worth knowing

| Concern | How it behaves |
|---|---|
| **Latency** | Browser-driven, so seconds not milliseconds — the extension's own notes record 22.3s for one cloud run. `JOB_TIMEOUT_S` defaults to 75s. This is why speculative prefetch is mandatory rather than an optimisation. |
| **Partial results** | If one platform stalls, the bridge returns what did land rather than waiting for the straggler. On a live call, three prices now beat four prices later. |
| **Duplicate routes** | One in-flight job per route. A prefetch and a real ask for the same route join the same job instead of opening two tabs. |
| **Cache** | 15 min per exact route. This is the mechanism that makes a prefetched answer feel instant. |
| **Chrome not running** | `/v1/search` returns `no_browser` immediately rather than making the agent wait out the timeout mid-conversation. |
| **Failures** | Always typed (`timeout`, `no_browser`, `no_results`, `all_platforms_failed`). The agent says "couldn't fetch" and **never invents results** (spec §10). |
| **Focus** | The entry tab opens in a **minimized, unfocused window**, and is always closed afterwards — a leaked window per search would pile up invisibly. |
| **Deep links** | `null`. The extension captures *search* responses, which carry no booking URLs. A fabricated one would be worse than none. |

## Rate limiting — the real operational risk

Every search drives four aggregators from your residential IP. The extension's
own `SECURITYPLAN.md` names aggregator anti-bot teams as adversary number one.
A prefetching agent can generate far more searches per hour than a human
shopping, which is exactly the signal those teams look for.

Before this runs on real calls, decide a per-hour ceiling and enforce it in the
bridge. It isn't implemented yet and it is the most likely way to get blocked.

## Tests

```sh
.venv/bin/python -m pytest tests/test_flight_bridge.py -q
```

15 tests, no browser and no network: a fake extension client claims jobs and
answers them. Covers cross-platform merging, both parser field spellings,
dropping unpriced flights, trimming to three options, cheapest-first ordering,
cache hits, duplicate-route joining, typed failures, and token enforcement.

The only thing they can't cover is real Chrome actually returning results — that
needs one live run against the extension.
