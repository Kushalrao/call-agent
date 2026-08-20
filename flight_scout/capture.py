"""Drive a real Chrome over the DevTools Protocol and capture one XHR body.

This replaces the extension for the entry search. The insight is that the
extension's `inpage.js` and this module want the same thing — the site's own
flight-search response — and CDP can read it from outside the page, so nothing
has to be installed into the browser.

It is still a *real* Chrome: real TLS fingerprint, real rendering, real cookies
from a persistent profile. That is what matters for getting a response at all.

No third-party automation library. The slice of CDP needed here is small enough
that a dependency would cost more than it saves.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import websockets

CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]


class CaptureError(RuntimeError):
    pass


@dataclass
class Capture:
    url: str
    status: int
    body: str
    elapsed_ms: int
    requests_seen: int


def chrome_binary() -> str:
    for p in CHROME_PATHS:
        if p and Path(p).exists():
            return p
    raise CaptureError("no Chrome or Chromium found")


def _debug_json(port: int, path: str) -> list | dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json{path}", timeout=5) as r:
        return json.loads(r.read())


def chrome_alive(port: int) -> bool:
    try:
        _debug_json(port, "/version")
        return True
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False


def ensure_chrome(*, port: int, profile_dir: Path, headless: bool = False) -> None:
    """Start Chrome with a debugging port if one isn't already listening.

    The profile is persistent on purpose: cookies and site reputation accumulate
    across runs, which is exactly what makes an aggregator treat the session as a
    person rather than a script.
    """
    if chrome_alive(port):
        return

    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        chrome_binary(),
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        # Keep the window out of the way without hiding it: aggregators gate
        # rendering on document.visibilityState, and a minimized window still
        # reports visible.
        "--window-position=0,3000",
        "--window-size=1280,900",
    ]
    if headless:
        # Not the default. Headless is a well-known bot signal, and the whole
        # point of using real Chrome is to not look like a bot.
        args.append("--headless=new")

    # start_new_session detaches Chrome into its own process group, so it is not
    # a child of whatever started it. Without this the browser dies with the
    # agent worker on every restart — and a restarted Chrome has a cold session,
    # which is precisely the state in which the aggregators time out. Warmth in
    # this profile is the whole reason it is persistent, and a browser that keeps
    # being killed can never accumulate any.
    subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    for _ in range(60):
        if chrome_alive(port):
            return
        time.sleep(0.5)
    raise CaptureError(f"Chrome did not open a debug port on {port}")


async def capture_xhr(
    *,
    navigate_to: str,
    url_pattern: str,
    port: int,
    timeout_s: float = 90.0,
    settle_ms: int = 400,
) -> Capture:
    """Navigate a tab and return the body of the first response matching
    `url_pattern`.

    The body is requested only after `Network.loadingFinished` for that exact
    requestId. Asking earlier returns an empty string — Chrome has not finished
    buffering — and asking on every event produces a storm of empty replies.
    """
    pattern = re.compile(url_pattern, re.I)
    pages = [t for t in _debug_json(port, "/list") if t.get("type") == "page"]
    if not pages:
        raise CaptureError("no page target in Chrome")
    target = pages[0]

    started = time.monotonic()
    request_ids: dict[str, tuple[str, int]] = {}  # requestId -> (url, status)
    finished: set[str] = set()
    seen = 0

    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=None) as ws:
        msg_id = 0

        async def send(method: str, params: dict | None = None) -> int:
            nonlocal msg_id
            msg_id += 1
            await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
            return msg_id

        await send("Network.enable")
        await send("Page.enable")
        await asyncio.sleep(0.2)
        await send("Page.navigate", {"url": navigate_to})

        body_request_id: int | None = None
        deadline = started + timeout_s

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)

            # Reply to our getResponseBody call.
            if body_request_id is not None and msg.get("id") == body_request_id:
                body = (msg.get("result") or {}).get("body") or ""
                if body:
                    url, status = next(
                        (v for k, v in request_ids.items() if k in finished and pattern.search(v[0])),
                        (navigate_to, 0),
                    )
                    return Capture(
                        url=url,
                        status=status,
                        body=body,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        requests_seen=seen,
                    )
                body_request_id = None  # empty: keep waiting for another match

            method = msg.get("method")
            if method == "Network.responseReceived":
                seen += 1
                p = msg["params"]
                request_ids[p["requestId"]] = (p["response"]["url"], p["response"]["status"])

            elif method == "Network.loadingFinished":
                rid = msg["params"]["requestId"]
                finished.add(rid)
                entry = request_ids.get(rid)
                if entry and pattern.search(entry[0]) and body_request_id is None:
                    # Small settle delay: loadingFinished can arrive marginally
                    # before the body is retrievable for large responses.
                    await asyncio.sleep(settle_ms / 1000)
                    body_request_id = await send(
                        "Network.getResponseBody", {"requestId": rid}
                    )

    raise CaptureError(
        f"no response matching {url_pattern!r} within {timeout_s:.0f}s "
        f"({seen} requests observed)"
    )
