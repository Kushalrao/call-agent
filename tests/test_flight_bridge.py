"""Bridge tests, with a fake extension standing in for Chrome.

The point: the bridge's contract, the normalizer, and the failure modes can all
be verified with no browser, no network, and no aggregator involved. Only the
final hop — real Chrome actually returning results — needs the real thing.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from asgi_lifespan import LifespanManager

from flight_bridge import main as bridge_main
from flight_bridge.normalize import to_widget_payload

TOKEN = bridge_main.BRIDGE_TOKEN
AUTH = {"X-Bridge-Token": TOKEN}


@pytest.fixture(autouse=True)
def _reset_bridge(monkeypatch):
    monkeypatch.setattr(bridge_main, "POLL_HOLD_S", 0.4)
    monkeypatch.setattr(bridge_main, "JOB_TIMEOUT_S", 3.0)
    b = bridge_main.bridge
    b.pending = asyncio.Queue()
    b.jobs.clear()
    b.cache.clear()
    b.inflight.clear()
    b.last_browser_seen = None
    yield


@pytest.fixture
def browser_online():
    """Simulate the extension having polled recently.

    The real extension long-polls continuously, so by the time anyone is on a
    call `browser_connected` is already true. Tests that omit this fixture are
    asserting the Chrome-not-running path on purpose.
    """
    import time as _time
    bridge_main.bridge.last_browser_seen = _time.monotonic()
    yield


@pytest.fixture
async def client():
    async with LifespanManager(bridge_main.app):
        transport = httpx.ASGITransport(app=bridge_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as c:
            yield c


# --- fixtures mirroring the extension's real record shape -------------------


def _flight(num: str, depart: str, price: int, *, carrier="IndiGo", stops=0, dur=155):
    """Shaped like lib/parsers/* output."""
    return {
        "travelOptionId": f"{num}-{depart}",
        "airlines": [carrier],
        "flightNumbers": [num],
        "origin": "BLR",
        "destination": "DEL",
        "departIso": depart,
        "arriveIso": "2026-12-07T11:05+05:30",
        "durationMin": dur,
        "stops": stops,
        "minPrice": price,
    }


def _segment_flight(code: str, number: str, depart: str, price: int):
    """The other spelling: segments-based, as `safeFlight()` reads it."""
    return {
        "travelOptionId": f"{code}{number}-{depart}",
        "segments": [{"airlineCode": code, "airlineName": "Vistara", "flightNumber": number}],
        "origin": "BLR",
        "destination": "DEL",
        "departTime": depart,
        "arriveTime": "2026-12-07T09:40+05:30",
        "durationTotalMinutes": 145,
        "stops": 0,
        "minPrice": price,
    }


def _record(**site_overrides):
    sites = {
        "cleartrip": {"status": "complete", "flights": [_flight("6E2345", "2026-12-07T08:30+05:30", 5400)]},
        "mmt": {"status": "complete", "flights": [_flight("6E2345", "2026-12-07T08:30+05:30", 5100)]},
        "ixigo": {"status": "error", "error": "turnstile", "flights": []},
        "skyscanner": {"status": "complete", "flights": [_flight("6E2345", "2026-12-07T08:30+05:30", 5900)]},
    }
    sites.update(site_overrides)
    return {
        "id": "cs_test_1",
        "route": {"from": "BLR", "to": "DEL", "date": "2026-12-07", "returnDate": None},
        "sites": sites,
    }


async def _fake_extension(client: httpx.AsyncClient, record, *, error=None):
    """Claim one job and answer it, the way the extension module does."""
    offer = (await client.get("/v1/jobs/next", headers=AUTH)).json()
    assert offer["job_id"], "no job was offered"
    body = {"error": error} if error else {"record": record}
    await client.post(f"/v1/jobs/{offer['job_id']}/result", headers=AUTH, json=body)
    return offer


# --- normalizer: per-site, no merging ------------------------------------
#
# Merging/deduping across platforms is `lib/analysis/dedupe.js` in the Watermelon
# extension (fingerprint + 10-min bucket + codeshare awareness). This module
# must NOT do its own, so these tests assert results stay separated by platform.


def test_platforms_stay_separate():
    """The same flight from three platforms must remain three entries, one per
    platform — not one merged entry."""
    payload = to_widget_payload(_record())
    plats = payload["platforms"]
    assert set(plats) == {"Cleartrip", "MakeMyTrip", "Ixigo", "Skyscanner"}
    assert plats["Cleartrip"]["cheapest"] == 5400
    assert plats["MakeMyTrip"]["cheapest"] == 5100
    assert plats["Skyscanner"]["cheapest"] == 5900
    # One option each, still carrying its own price — nothing collapsed.
    for name in ("Cleartrip", "MakeMyTrip", "Skyscanner"):
        assert len(plats[name]["options"]) == 1


def test_failed_platform_reports_why():
    plats = to_widget_payload(_record())["platforms"]
    assert plats["Ixigo"]["status"] == "error"
    assert plats["Ixigo"]["error"] == "turnstile"
    assert plats["Ixigo"]["options"] == []
    assert plats["Ixigo"]["cheapest"] is None


def test_both_parser_spellings_are_understood():
    """MMT emits departIso/durationMin; other parsers go through
    segments/departTime. Both must normalize or a platform silently contributes
    nothing."""
    record = _record(
        mmt={"status": "complete",
             "flights": [_segment_flight("UK", "810", "2026-12-07T07:15+05:30", 6200)]}
    )
    opt = to_widget_payload(record)["platforms"]["MakeMyTrip"]["options"][0]
    assert opt["flight"] == "UK810"
    assert opt["carrier"] == "Vistara"
    assert opt["duration_min"] == 145
    assert opt["price"]["amount"] == 6200


def test_unpriced_and_undated_flights_are_dropped_not_invented():
    record = _record(
        cleartrip={
            "status": "complete",
            "flights": [
                {"flightNumbers": ["6E999"], "departIso": "2026-12-07T06:00+05:30"},  # no price
                {"flightNumbers": ["6E888"], "minPrice": 4000},                       # no depart
                _flight("6E777", "2026-12-07T05:00+05:30", 4200),
            ],
        }
    )
    ct = to_widget_payload(record)["platforms"]["Cleartrip"]
    assert ct["raw_flights"] == 3
    assert ct["usable_flights"] == 1
    assert ct["options"][0]["flight"] == "6E777"


def test_widget_payload_matches_spec_shape():
    payload = to_widget_payload(_record())
    assert payload["route"] == {"origin": "BLR", "destination": "DEL"}
    assert payload["date_range"] == ["2026-12-07"]
    opt = payload["platforms"]["Cleartrip"]["options"][0]
    for key in ("carrier", "flight", "depart", "arrive", "stops", "duration_min", "price"):
        assert key in opt, f"spec section 7 field missing: {key}"
    assert opt["price"] == {"amount": 5400, "currency": "INR"}
    # Captured search responses carry no booking URLs; a fabricated one would be
    # worse than none.
    assert opt["deeplink"] is None


def test_payload_is_trimmed_per_platform():
    """Section 6: the model must never see raw API JSON."""
    many = [_flight(f"6E{i:04d}", f"2026-12-07T{i % 24:02d}:00+05:30", 4000 + i) for i in range(30)]
    ct = to_widget_payload(_record(cleartrip={"status": "complete", "flights": many}))["platforms"]["Cleartrip"]
    assert ct["raw_flights"] == 30
    assert ct["usable_flights"] == 30
    assert len(ct["options"]) == 3
    prices = [o["price"]["amount"] for o in ct["options"]]
    assert prices == sorted(prices), "options should be cheapest-first"


def test_missing_platform_is_reported_not_omitted():
    """A platform the extension never spawned must still appear, so the agent can
    say what it did and did not check."""
    record = {"id": "x", "route": {"from": "BLR", "to": "DEL", "date": "2026-12-07"},
              "sites": {"cleartrip": {"status": "complete", "flights": []}}}
    plats = to_widget_payload(record)["platforms"]
    assert plats["Skyscanner"]["status"] is None
    assert plats["Skyscanner"]["raw_flights"] == 0


# --- bridge contract --------------------------------------------------------


async def test_full_round_trip(client: httpx.AsyncClient, browser_online):
    """Agent asks, fake extension answers, agent gets normalized results."""
    search = asyncio.create_task(
        client.post(
            "/v1/search",
            headers=AUTH,
            json={"origin": "BLR", "destination": "DEL", "depart_date": "2026-12-07"},
        )
    )
    await asyncio.sleep(0.05)  # let the job reach the queue
    await _fake_extension(client, _record())

    res = (await search).json()
    assert res["ok"] is True
    assert res["cached"] is False
    assert res["payload"]["platforms"]["MakeMyTrip"]["cheapest"] == 5100


async def test_second_identical_search_is_served_from_cache(client: httpx.AsyncClient, browser_online):
    """This is the mechanism that makes a prefetch feel instant."""
    body = {"origin": "BLR", "destination": "DEL", "depart_date": "2026-12-07"}
    task = asyncio.create_task(client.post("/v1/search", headers=AUTH, json=body))
    await asyncio.sleep(0.05)
    await _fake_extension(client, _record())
    assert (await task).json()["ok"] is True

    again = (await client.post("/v1/search", headers=AUTH, json=body)).json()
    assert again["cached"] is True
    assert again["elapsed_ms"] == 0


async def test_concurrent_identical_searches_open_one_browser_job(client: httpx.AsyncClient, browser_online):
    """A prefetch and a real ask for the same route must not race two tabs."""
    body = {"origin": "BLR", "destination": "DEL", "depart_date": "2026-12-07"}
    t1 = asyncio.create_task(client.post("/v1/search", headers=AUTH, json=body))
    t2 = asyncio.create_task(client.post("/v1/search", headers=AUTH, json=body))
    await asyncio.sleep(0.05)

    await _fake_extension(client, _record())

    r1, r2 = (await t1).json(), (await t2).json()
    assert r1["ok"] and r2["ok"]
    # Only one job was ever created, so only one Cleartrip tab was opened.
    assert len(bridge_main.bridge.jobs) == 1


async def test_no_browser_fails_fast_rather_than_waiting(client: httpx.AsyncClient):
    """With Chrome not running, the agent must be told immediately — not left
    to wait out the full timeout mid-conversation."""
    res = (
        await client.post(
            "/v1/search",
            headers=AUTH,
            json={"origin": "BLR", "destination": "DEL", "depart_date": "2026-12-07"},
        )
    ).json()
    assert res["ok"] is False
    assert res["error"]["kind"] == "no_browser"


async def test_extension_error_becomes_typed_failure(client: httpx.AsyncClient, browser_online):
    task = asyncio.create_task(
        client.post(
            "/v1/search",
            headers=AUTH,
            json={"origin": "BLR", "destination": "DEL", "depart_date": "2026-12-07"},
        )
    )
    await asyncio.sleep(0.05)
    await _fake_extension(client, None, error="turnstile on every platform")

    res = (await task).json()
    assert res["ok"] is False
    assert res["error"]["kind"] == "all_platforms_failed"
    assert "turnstile" in res["error"]["detail"]


async def test_empty_results_are_reported_not_faked(client: httpx.AsyncClient, browser_online):
    task = asyncio.create_task(
        client.post(
            "/v1/search",
            headers=AUTH,
            json={"origin": "BLR", "destination": "DEL", "depart_date": "2026-12-07"},
        )
    )
    await asyncio.sleep(0.05)
    empty = _record(
        cleartrip={"status": "complete", "flights": []},
        mmt={"status": "complete", "flights": []},
        ixigo={"status": "complete", "flights": []},
        skyscanner={"status": "complete", "flights": []},
    )
    await _fake_extension(client, empty)

    res = (await task).json()
    assert res["ok"] is False
    assert res["error"]["kind"] == "no_results"
    assert res["payload"] is None


async def test_token_is_required_everywhere(client: httpx.AsyncClient):
    """This service can make a browser navigate. It must not be drivable by
    anything that can reach the port."""
    unauthorized = [
        await client.post("/v1/search", json={}),
        await client.get("/v1/jobs/next"),
        await client.post("/v1/jobs/abc/result", json={}),
    ]
    for res in unauthorized:
        assert res.status_code == 401, (
            f"{res.request.url.path} accepted a request with no token "
            f"(got {res.status_code})"
        )


async def test_route_sent_to_extension_matches_lib_route_shape(client: httpx.AsyncClient, browser_online):
    """The extension feeds this straight into buildSearchUrl(), so the key names
    must match lib/route.js exactly."""
    task = asyncio.create_task(
        client.post(
            "/v1/search",
            headers=AUTH,
            json={
                "origin": "blr",
                "destination": "del",
                "depart_date": "2026-12-07",
                "return_date": "2026-12-13",
                "adults": 2,
                "cabin": "BUSINESS",
            },
        )
    )
    await asyncio.sleep(0.05)
    offer = (await client.get("/v1/jobs/next", headers=AUTH)).json()
    route = offer["route"]
    assert route["from"] == "BLR" and route["to"] == "DEL"  # uppercased for the URL builders
    assert route["date"] == "2026-12-07"
    assert route["returnDate"] == "2026-12-13"
    assert route["tripType"] == "ROUND_TRIP"
    assert route["adults"] == 2
    assert route["cabin"] == "BUSINESS"
    # intl is intentionally absent: route.js derives it, and Cleartrip 400s on a
    # mismatch.
    assert "intl" not in route
    assert offer["entry_site"] == "cleartrip"

    await client.post(f"/v1/jobs/{offer['job_id']}/result", headers=AUTH, json={"error": "done"})
    await task


async def test_healthz_reports_browser_presence(client: httpx.AsyncClient):
    before = (await client.get("/healthz")).json()
    assert before["browser_connected"] is False

    poll = asyncio.create_task(client.get("/v1/jobs/next", headers=AUTH))
    await asyncio.sleep(0.05)
    after = (await client.get("/healthz")).json()
    assert after["browser_connected"] is True

    poll.cancel()
