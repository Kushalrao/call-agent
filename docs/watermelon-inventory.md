# Watermelon extension — what already exists

Inventory taken 2026-08-21 from `Kushalrao/chrome-flight-extension` @ `d78d4ee`.
79 files, ~9,400 lines of JS. **Read this before writing anything flight-related.**

The short version: the extension is not a scraper with a comparison table bolted
on. It is a complete pipeline — capture → parse → cross-search → dedupe → risk-tag
→ stats → token-dense encode → LLM analysis → Q&A. Most of what a flight agent
needs already exists here, tuned, and hands-free should call it rather than
reproduce it.

---

## 1. How data gets in

| File | Does |
|---|---|
| `inpage.js` (327) | Runs in the **MAIN world** at `document_start` and monkey-patches `window.fetch` and `XMLHttpRequest.prototype.open/send`. Posts every response to the content script via `postMessage`. Also hooks `history.pushState/replaceState` to detect SPA navigation. |
| `content.js` (872) | Buffers captures, detects "a search just happened", triggers cross-search, snapshots page state for diagnostics, and can **click a site's Search button** (full pointer/mouse sequence, because React handlers ignore synthetic clicks). Has cascade prevention so a spawned tab doesn't trigger its own cross-search. |
| `lib/iframeRelay.js` (135) | Same relay but for iframe contexts — kept separate from `content.js` because that bundle is heavy. |
| `lib/sites.js` | `FLIGHT_API_PATTERNS` per site: the exact XHR each SPA fires. Also `SEARCH_PAGE_PATTERNS` and `REVIEW_API_PATTERNS`. |

**Key insight: it does not scrape rendered HTML for prices.** It reads each
site's own JSON/SSE API response. `lib/scrape.js` (586) exists as a DOM fallback
but is not the primary path.

## 2. Parsers — one per site

`lib/parsers/{cleartrip,mmt,ixigo,skyscanner,indigo}.js`

Dependency-free IIFEs that attach to `self.AFG.parsers.<site>`, each exposing
`parse(body)` → `{ok, flights[], error}`. Skyscanner additionally exposes
`mergeFlights()` because it polls and results arrive incrementally.

Per-site shape they have to cope with:

| Site | Response |
|---|---|
| Cleartrip | one JSON POST — `cards.J1` + `subTravelOptions` + `fares` |
| MMT | SSE, event payloads **base64 + gzip** (`DecompressionStream`) |
| Ixigo | `/search/stream` then `/search/poll`, repeatedly |
| Skyscanner | `create` then `poll/<token>`, many times, merged |

IndiGo exists but is **out of scope** (dropped 2026-08-21).

## 3. Cross-search — the fan-out

`lib/crossSearch.js` (747) — `startCrossSearch({route, fromSite, fromTabId})`.
Dedupes by `routeHash`, builds target URLs for every *other* site, spawns them,
tracks per-site status with alarm-based timeouts, accumulates polling sites.

`lib/offscreenSpawn.js` (305) — **the other sites load in iframes inside an
offscreen document, not tabs.** Per `migrationplan.md` this replaced the old
visible-tab spawner so there is no tab clutter or dock blink. Two consequences
worth knowing:

- Counting browser tabs will **not** show the fan-out happening.
- Iframes don't suffer the `document.visibilityState` gating that background tabs
  do, so `foregroundBootstrapTabs()` is now a no-op.

`lib/route.js` (248) — pure. `parseSearchUrl(url) → Route`,
`buildSearchUrl(site, route) → url`, `routeHash()`, and `detectIntl()` off an
Indian-airport set (Cleartrip 400s if the `intl` flag disagrees with the route).

## 4. The analysis pipeline — this is the part I nearly rebuilt

| File | Does |
|---|---|
| `lib/analysis/dedupe.js` (129) | Cross-platform dedupe. Strict fingerprint = sorted flight numbers + 10-min departure bucket + origin/dest, with a fuzzy fallback. Produces one canonical flight carrying `offers[]` per platform. |
| `lib/analysis/codeshare.js` (54) | Alliance table, so the same physical flight sold under two airline codes merges. |
| `lib/analysis/risk.js` (100) | Deterministic risk flags before any LLM sees the data: `TL`, `LL`, `RE`, `NB`, `NR`. |
| `lib/analysis/stats.js` (55) | Per-route price/duration distribution with percentiles. |
| `lib/analysis/encode.js` (170) | **Token-dense encoding** into DIRECT and CONNECTING tables with stable row ids (`Da3`), grouped by airline. This is what keeps the LLM call affordable. |
| `lib/analysis/pipeline.js` (123) | cs record → `{encodedTables, stats, fingerprint, idMap}`. Pure, no LLM. |
| `lib/analysis/phaseA.js` (195) | Unprompted route analysis via Claude **tool-use with a forced schema** — picks, avoids, observations. |
| `lib/analysis/phaseB.js` (101) | Follow-up Q&A using Phase A's artifact as compressed working memory. |
| `lib/analysis/questionRouter.js` (55) | Classifies a question: `recommend` vs `filter`. |
| `lib/analysis/filter.js` (128) | Runs deterministic predicates ("under ₹40k", "non-stop", time windows) **in code, no LLM tokens**. |
| `lib/analysis/trigger.js` (145) | Fires Phase A when a cross-search reaches all-terminated. Idempotent. |
| `lib/analysis/claude.js` (97) | Anthropic wrapper. Deliberately non-streaming for Phase A: tool-use blocks arrive in one chunk anyway. |
| `lib/ranking.js` (214) | Pre-LLM shortlist: value score, arrival civility, departure practicality, trap detection. Cut ~40× cost by sending 15–25 representative flights instead of everything. |

`lib/config.js` — models are `claude-haiku-4-5-20251001` for both phases,
`timeBucketMinutes: 10`, `phaseAConcurrent: 2`.

## 5. Storage

`lib/storage.js` (221) — IndexedDB `ai-flight-guide` v2, stores `captures` and
`crossSearches` (keyPath `id`; indexes `triggeredAt`, `routeHash`,
`overallStatus`). `findRecentCrossSearch(routeHash, ttlMs)` is the natural
lookup for "did we just search this route".

## 6. UI and the rest

`lib/panel.js` (2155) in-page panel — comparison grid, per-site tabs, Phase A
render, detail-match, settings. `lib/comparisonTable.js` (95) builds
flight × platform rows as a pure transform. `lib/timeAnalysis.js` (364) SVG
time-of-day vs price chart. `lib/cursorInput.js` (627) press `I` for a
cursor-following question box → Phase B. `lib/detail/*` (identity/match/autoPick/
handler) — when the user opens a flight's detail page, find the same flight on the
other sites; `autoPick.js` can click the matching card. `lib/reviewOffers.js` (160)
parses review-page coupons and works out the best one a normal user can actually
redeem (UPI or all-payment-modes, not bank-card-specific).

## 7. The extension's message API

`chrome.runtime.sendMessage` handlers in `background.js`:

```
search-detected           flight-response        capture-saved
list-cross-searches       get-cross-search       force-cross-search
detail-page-detected      detail-page-clear      cursor-ask
spawned-tab-ready         spawned-tab-stage      spawned-tab-snapshot
spawned-tab-clicked-search  spawned-xhr          am-i-spawned
offscreen-relay-ready     iframe-net-trace       indigo-srp-captured
```

## 8. Plans already written

- `migrationplan.md` — spawn-tab → offscreen-iframe. Done.
- `futurecloud.md` — cloud browsers (Steel/Scrapfly) via Playwright. **Marked
  mostly obsolete** by the offscreen migration. Contains the one hard latency
  number: *Steel × Ixigo, 732 KB body, 22.3s*.
- `SECURITYPLAN.md` — hardening against reverse engineering and aggregator
  fingerprinting. Names aggregator anti-bot teams as adversary #1.

---

## What this means for hands-free

**Do not build:** URL building, per-site capture, parsing, cross-search
orchestration, dedupe, codeshare merging, risk flags, stats, LLM-friendly
encoding, ranking/shortlisting, price-and-stops filtering, or flight Q&A. All of
it exists and is tuned.

**What genuinely doesn't exist**, and is all hands-free needs:

1. **A way to start a search with no human on a page.** The extension triggers off
   a *user* landing on a results page. Verified working: navigate a tab to
   `buildSearchUrl('cleartrip', route)` and the existing `search-detected` →
   `startCrossSearch` path fans out on its own.
2. **A way to get the assembled record out to the agent.** The record lives in
   IndexedDB. Reading it works (verified: 245 Cleartrip + 151 Ixigo flights).
3. **Route inference from a phone conversation** — genuinely new, and the actual
   hands-free product.

**Kept per-site, not merged** (Kushal, 2026-08-21): `dedupe.js` already merges
across platforms when that's wanted. The bridge normalizer must not do its own.

## Verified by running it, 2026-08-21

- Loading the unpacked extension from the command line is **impossible in Chrome
  151** (`--load-extension` removed). CDP `Extensions.loadUnpacked` **does** work.
- Its MV3 service worker only starts once a **matched page** loads — opening
  `about:blank` or `popup.html` is not enough.
- Driving it: navigate to a Cleartrip results URL → `search-detected` →
  `crossSearch created` → offscreen iframes for the other sites → `all-terminated`.
- `list-cross-searches` returned 0 items while IndexedDB held 2 records. Reading
  the store directly worked. Possible bug in that handler, unconfirmed.
- **Profile warmth is the whole game.** On a cold profile (no cookies, no
  history) MMT timed out 4/4 runs and the cross-search never went past
  `partial`. On a warm profile — same code, *original* 75s/50s timeouts — all
  three platforms complete in **16–18 seconds**:

  | route | total | cleartrip | ixigo | mmt |
  |---|---|---|---|---|
  | BLR→DEL 2026-09-04 | 656 | 244 | 119 | 293 |
  | BLR→DEL 2026-09-11 | 627 | 240 | 120 | 267 |
  | BLR→DEL 2026-09-18 | 652 | 240 | 153 | 259 |

  Raising the timeouts was **not** what fixed it — a control run with the
  original values passed twice. The extension needs no changes. This is the
  practical form of "these sites work because of a real browser session":
  automation must reuse a profile, never start clean each time.
- Skyscanner is not spawned **by design** — `ALL_SITES` in `crossSearch.js` and
  `ANALYZED_SITES` in `pipeline.js` are both `['cleartrip','ixigo','mmt']`. It has
  a parser and can be an entry site, but is excluded from the fan-out.
- Timing on a warm profile: entry site (Cleartrip) lands in **~2s**; all three in
  **~16s**. MMT is dispatched first and sequentially (`SITE_PRIORITY = {mmt:0,
  cleartrip:1, ixigo:2}`), so it gates the others — which is fine when it works.
