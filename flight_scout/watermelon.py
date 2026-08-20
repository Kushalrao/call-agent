"""Drive the Watermelon extension from outside the browser. No changes to it.

The extension already does everything except *start* a search without a human on
a page. So that is all this does:

    1. real Chrome with the extension loaded (CDP `Extensions.loadUnpacked`)
    2. navigate a tab to the Cleartrip results URL for the route
    3. the extension's own `search-detected` -> `startCrossSearch` fans out to the
       other sites in offscreen iframes
    4. wait for *its* completion signal
    5. read the assembled record

Everything in step 3 and 4 belongs to the extension. In particular this module
does **not** decide when a cross-search is finished — `lib/crossSearch.js` owns
that, and it publishes the answer two ways:

    push:  broadcast({type: 'cross-search-update', id, record}) on every change
    state: record.overallStatus  in_progress -> complete | partial | error | abandoned

We subscribe to the push and read `overallStatus`. Cross-searches can take a
while (MMT in particular), and that is fine — the extension's own per-site alarm
timeouts decide when to give up, not us.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets

from .capture import CaptureError, chrome_alive, ensure_chrome

CHROME_PORT = int(os.environ.get("WATERMELON_CHROME_PORT", "9224"))
PROFILE_DIR = Path(
    os.environ.get("WATERMELON_PROFILE_DIR", Path.home() / ".hands-free" / "watermelon-profile")
)
EXTENSION_DIR = Path(
    os.environ.get(
        "WATERMELON_EXTENSION_DIR",
        Path.home() / "Desktop" / "chrome-flight-extension",
    )
)

# lib/analysis/pipeline.js — the extension's definition, not ours.
TERMINAL_SITE_STATUSES = {"complete", "partial", "error", "timeout", "self"}
# lib/crossSearch.js — record-level lifecycle.
TERMINAL_OVERALL = {"complete", "partial", "error", "abandoned"}


class WatermelonError(RuntimeError):
    pass


@dataclass
class CrossSearchResult:
    ok: bool
    record: dict[str, Any] | None = None
    error: str | None = None
    error_kind: str | None = None
    elapsed_ms: int | None = None
    updates_seen: int = 0  # poll count
    per_site: dict[str, Any] = field(default_factory=dict)


def _dbg(path: str, method: str = "GET", port: int = CHROME_PORT) -> Any:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/json{path}", method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


class _Cdp:
    """Minimal CDP session over one websocket."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._id = 0

    async def call(self, method: str, params: dict | None = None, timeout: float = 40.0) -> dict:
        self._id += 1
        mid = self._id
        await self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=deadline - time.monotonic())
            msg = json.loads(raw)
            if msg.get("id") == mid:
                return msg
        raise WatermelonError(f"CDP {method} timed out")

    async def evaluate(self, expression: str, timeout: float = 40.0) -> Any:
        msg = await self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": int(timeout * 1000),
            },
            timeout=timeout + 5,
        )
        details = msg.get("result", {}).get("exceptionDetails")
        if details:
            raise WatermelonError(f"evaluate failed: {details.get('text')}")
        return msg.get("result", {}).get("result", {}).get("value")


async def _browser_session() -> Any:
    return await websockets.connect(
        _dbg("/version")["webSocketDebuggerUrl"], max_size=None
    )


async def ensure_extension(extension_dir: Path = EXTENSION_DIR) -> str:
    """Load the unpacked extension and return its id.

    `--load-extension` was removed in Chrome 151; the CDP Extensions domain is
    the working route. Loading an already-loaded extension returns the same id,
    so this is safe to call every time.
    """
    if not (extension_dir / "manifest.json").exists():
        raise WatermelonError(
            f"no extension at {extension_dir} — set WATERMELON_EXTENSION_DIR"
        )
    async with await _browser_session() as ws:
        cdp = _Cdp(ws)
        msg = await cdp.call("Extensions.loadUnpacked", {"path": str(extension_dir)})
        if msg.get("error"):
            raise WatermelonError(f"loadUnpacked failed: {msg['error']}")
        return msg["result"]["id"]


async def _extension_page(ext_id: str) -> dict:
    """Any page inside the extension — needed only so we can read its
    IndexedDB, which is same-origin to the extension."""
    for _ in range(3):
        pages = [
            t for t in _dbg("/list")
            if t.get("type") == "page" and ext_id in t.get("url", "")
        ]
        if pages:
            return pages[0]
        async with await _browser_session() as bws:
            await _Cdp(bws).call(
                "Target.createTarget", {"url": f"chrome-extension://{ext_id}/popup.html"}
            )
        await asyncio.sleep(1.5)
    raise WatermelonError("could not open an extension page")


_READ_RECORDS_JS = """new Promise((res) => {
  const q = indexedDB.open('ai-flight-guide', 2);
  q.onerror = () => res('[]');
  q.onsuccess = () => {
    let tx;
    try { tx = q.result.transaction('crossSearches', 'readonly'); }
    catch (e) { return res('[]'); }
    const g = tx.objectStore('crossSearches').getAll();
    g.onsuccess = () => res(JSON.stringify(g.result || []));
    g.onerror = () => res('[]');
  };
})"""


def _matches(record: dict[str, Any], origin: str, destination: str, depart_date: str) -> bool:
    rt = record.get("route") or {}
    return (
        (rt.get("from") or "").upper() == origin.upper()
        and (rt.get("to") or "").upper() == destination.upper()
        and rt.get("date") == depart_date
    )


async def read_records(cdp: "_Cdp") -> list[dict[str, Any]]:
    """Read the extension's `crossSearches` store.

    This is the authoritative state. The extension also broadcasts
    `cross-search-update` on every change, but a broadcast can be missed (the
    listening page may not exist yet, or the final one lands after we stop
    reading) — and a missed broadcast made a perfectly good search look empty.
    """
    raw = await cdp.evaluate(_READ_RECORDS_JS)
    try:
        rows = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [r for r in rows if isinstance(r, dict)]


async def cross_search(
    origin: str,
    destination: str,
    depart_date: str,
    *,
    return_date: str | None = None,
    adults: int = 1,
    children: int = 0,
    infants: int = 0,
    cabin: str = "ECONOMY",
    timeout_s: float = 150.0,
    wait_for_all: bool = False,
    extension_dir: Path = EXTENSION_DIR,
) -> CrossSearchResult:
    """`wait_for_all=False` returns as soon as the entry site has landed — right
    for a live call, where three prices now beat four prices in ninety seconds.
    Set it True to wait for every platform (or the extension's own timeouts)."""
    started = time.monotonic()

    try:
        ensure_chrome(port=CHROME_PORT, profile_dir=PROFILE_DIR)
    except CaptureError as exc:
        return CrossSearchResult(ok=False, error=str(exc), error_kind="no_browser")

    try:
        ext_id = await ensure_extension(extension_dir)
    except WatermelonError as exc:
        return CrossSearchResult(ok=False, error=str(exc), error_kind="no_extension")

    route = {
        "from": origin.upper(),
        "to": destination.upper(),
        "date": depart_date,
        "returnDate": return_date,
        "adults": adults,
        "children": children,
        "infants": infants,
        "cabin": cabin,
        "tripType": "ROUND_TRIP" if return_date else "ONE_WAY",
    }

    page = await _extension_page(ext_id)
    async with await websockets.connect(page["webSocketDebuggerUrl"], max_size=None) as ws:
        cdp = _Cdp(ws)
        await cdp.call("Runtime.enable")

        # Ask the extension to build the URL. lib/route.js owns URL construction
        # (including the intl flag Cleartrip validates), so calling it keeps one
        # source of truth instead of a second copy here.
        built = await cdp.evaluate(
            f"""(() => {{
              const r = (self.AFG && self.AFG.route) || null;
              if (!r || !r.buildSearchUrl) return null;
              const route = {json.dumps(route)};
              route.intl = r.detectIntl(route.from, route.to);
              return r.buildSearchUrl('cleartrip', route);
            }})()"""
        )
        if not built:
            from .cleartrip import search_url

            built = search_url(
                origin, destination, depart_date, return_date=return_date,
                adults=adults, children=children, infants=infants, cabin=cabin,
            )

        if "from=" not in built or "to=" not in built:
            raise WatermelonError(f"refusing to navigate a routeless URL: {built}")

        # Ignore any record for this route that already existed, so a stale one
        # is never mistaken for the result of this search.
        before = {r.get("id") for r in await read_records(cdp)}
        print(f"[watermelon] entry URL: {built}")

        # Open the entry search. Everything from here is the extension's own path:
        # search-detected -> startCrossSearch -> offscreen iframes for the rest.
        async with await _browser_session() as bws:
            entry_id = (
                await _Cdp(bws).call("Target.createTarget", {"url": built})
            )["result"]["targetId"]

        record: dict[str, Any] | None = None
        polls = 0
        deadline = started + timeout_s
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(2.5)
                polls += 1
                candidates = [
                    r for r in await read_records(cdp)
                    if _matches(r, origin, destination, depart_date)
                    and r.get("id") not in before
                ]
                if not candidates:
                    continue
                record = max(candidates, key=lambda r: r.get("triggeredAt") or "")

                if record.get("overallStatus") in TERMINAL_OVERALL:
                    break

                if not wait_for_all:
                    # Enough to speak with: the entry site has landed. The
                    # extension keeps filling the record in either way.
                    entry = (record.get("sites") or {}).get("cleartrip") or {}
                    if entry.get("status") == "complete" and (entry.get("flights") or []):
                        break
        finally:
            try:
                async with await _browser_session() as bws:
                    await _Cdp(bws).call("Target.closeTarget", {"targetId": entry_id})
            except Exception:  # noqa: BLE001
                pass

    elapsed = int((time.monotonic() - started) * 1000)

    if record is None:
        return CrossSearchResult(
            ok=False,
            error=f"extension published no cross-search for this route in {timeout_s:.0f}s",
            error_kind="no_cross_search",
            elapsed_ms=elapsed,
            updates_seen=polls,
        )

    per_site = {
        site: {
            "status": s.get("status"),
            "flights": len(s.get("flights") or []),
            "minPrice": s.get("minPrice"),
            "error": s.get("error"),
        }
        for site, s in (record.get("sites") or {}).items()
    }
    any_flights = any(v["flights"] for v in per_site.values())

    return CrossSearchResult(
        ok=any_flights,
        record=record,
        error=None if any_flights else "no flights on any platform",
        error_kind=None if any_flights else "no_results",
        elapsed_ms=elapsed,
        updates_seen=polls,
        per_site=per_site,
    )


async def _demo() -> None:
    import sys

    from flight_bridge.normalize import to_widget_payload

    o = sys.argv[1] if len(sys.argv) > 1 else "BLR"
    d = sys.argv[2] if len(sys.argv) > 2 else "DEL"
    date = sys.argv[3] if len(sys.argv) > 3 else "2026-10-12"

    print(f"cross-search {o} -> {d} on {date}\n(extension drives the fan-out; this can take a while)\n")
    res = await cross_search(o, d, date)
    print(f"ok={res.ok} elapsed={res.elapsed_ms}ms updates={res.updates_seen}")
    if res.record:
        print(f"overallStatus={res.record.get('overallStatus')} id={res.record.get('id','')[:8]}")
    for site, s in res.per_site.items():
        price = f"Rs {int(s['minPrice']):,}" if s.get("minPrice") else "-"
        print(f"  {site:<11} {str(s['status']):<10} flights={s['flights']:<5} {price:<12} {s.get('error') or ''}")
    if res.error:
        print("error:", res.error_kind, res.error)
    if res.record:
        payload = to_widget_payload(res.record)
        print("\nper-platform payload:")
        for name, p in payload["platforms"].items():
            if p["options"]:
                top = p["options"][0]
                print(f"  {name:<12} cheapest Rs {top['price']['amount']:,} "
                      f"{top['flight']} {top['depart'][11:16]} ({p['usable_flights']} usable)")


if __name__ == "__main__":
    asyncio.run(_demo())
