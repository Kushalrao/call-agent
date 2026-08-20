"""Phase 0 acceptance, as tests.

The authorization cases are the ones that must never regress: without them a
call_id is a room key.
"""

from __future__ import annotations

import httpx
import pytest

from conftest import auth, login, make_session, make_user


async def _start_call(client: httpx.AsyncClient, caller: dict, callee: dict) -> dict:
    r = await client.post(
        "/v1/calls", json={"callee_id": callee["user_id"]}, headers=auth(caller)
    )
    assert r.status_code == 201, r.text
    return r.json()


# --- happy path -------------------------------------------------------------


async def test_full_call_lifecycle(client: httpx.AsyncClient, pair) -> None:
    caller, callee = pair

    created = await _start_call(client, caller, callee)
    assert created["state"] == "RINGING"
    assert created["lk_token"]  # placeholder token without LiveKit creds
    call_id = created["call_id"]

    r = await client.post(f"/v1/calls/{call_id}/accept", headers=auth(callee))
    assert r.status_code == 200
    assert r.json()["state"] == "CONNECTING"

    # Neither side is ACTIVE until both have actually joined the room.
    r = await client.post(f"/v1/calls/{call_id}/joined", headers=auth(caller))
    assert r.json()["state"] == "CONNECTING"

    r = await client.post(f"/v1/calls/{call_id}/joined", headers=auth(callee))
    assert r.json()["state"] == "ACTIVE"

    r = await client.post(f"/v1/calls/{call_id}/end", headers=auth(callee))
    assert r.json()["state"] == "ENDED"


async def test_decline(client: httpx.AsyncClient, pair) -> None:
    caller, callee = pair
    call = await _start_call(client, caller, callee)

    r = await client.post(f"/v1/calls/{call['call_id']}/decline", headers=auth(callee))
    assert r.status_code == 200
    assert r.json()["state"] == "DECLINED"


async def test_end_is_idempotent(client: httpx.AsyncClient, pair) -> None:
    caller, callee = pair
    call = await _start_call(client, caller, callee)

    first = await client.post(f"/v1/calls/{call['call_id']}/end", headers=auth(caller))
    second = await client.post(f"/v1/calls/{call['call_id']}/end", headers=auth(caller))
    assert first.json()["state"] == second.json()["state"] == "ENDED"


# --- authorization ----------------------------------------------------------


async def test_third_party_cannot_see_or_join_a_call(
    client: httpx.AsyncClient, pair
) -> None:
    """The bug this guards against: call_id as a room key."""
    caller, callee = pair
    await make_user("Eve", "EVE-9Z1")
    eve = await login(client, "EVE-9Z1", "Eve")

    call = await _start_call(client, caller, callee)
    cid = call["call_id"]

    # 404, not 403 — a 403 would confirm the call exists.
    for path in ("accept", "decline", "end", "joined", "token"):
        r = await client.post(f"/v1/calls/{cid}/{path}", headers=auth(eve))
        assert r.status_code == 404, f"{path} leaked to a non-participant: {r.status_code}"

    r = await client.get(f"/v1/calls/{cid}", headers=auth(eve))
    assert r.status_code == 404


async def test_caller_cannot_accept_own_call(client: httpx.AsyncClient, pair) -> None:
    caller, callee = pair
    call = await _start_call(client, caller, callee)

    r = await client.post(f"/v1/calls/{call['call_id']}/accept", headers=auth(caller))
    assert r.status_code == 403


async def test_caller_cannot_decline(client: httpx.AsyncClient, pair) -> None:
    caller, callee = pair
    call = await _start_call(client, caller, callee)

    r = await client.post(f"/v1/calls/{call['call_id']}/decline", headers=auth(caller))
    assert r.status_code == 403


async def test_endpoints_require_auth(client: httpx.AsyncClient, pair) -> None:
    _, callee = pair
    assert (await client.post("/v1/calls", json={"callee_id": callee["user_id"]})).status_code == 401
    assert (await client.get("/v1/users/contacts")).status_code == 401


async def test_token_refresh_refused_on_terminal_call(
    client: httpx.AsyncClient, pair
) -> None:
    """An expired token must never be tradeable for access to a finished call."""
    caller, callee = pair
    call = await _start_call(client, caller, callee)
    cid = call["call_id"]

    ok = await client.post(f"/v1/calls/{cid}/token", headers=auth(caller))
    assert ok.status_code == 200

    await client.post(f"/v1/calls/{cid}/end", headers=auth(caller))
    after = await client.post(f"/v1/calls/{cid}/token", headers=auth(caller))
    assert after.status_code == 409


async def test_get_call_never_mints_a_token(client: httpx.AsyncClient, pair) -> None:
    caller, callee = pair
    call = await _start_call(client, caller, callee)

    r = await client.get(f"/v1/calls/{call['call_id']}", headers=auth(caller))
    assert r.status_code == 200
    assert r.json()["lk_token"] is None


# --- state machine ----------------------------------------------------------


async def test_cannot_accept_twice(client: httpx.AsyncClient, pair) -> None:
    caller, callee = pair
    call = await _start_call(client, caller, callee)

    assert (await client.post(f"/v1/calls/{call['call_id']}/accept", headers=auth(callee))).status_code == 200
    r = await client.post(f"/v1/calls/{call['call_id']}/accept", headers=auth(callee))
    assert r.status_code == 409


async def test_cannot_call_self(client: httpx.AsyncClient, pair) -> None:
    caller, _ = pair
    r = await client.post(
        "/v1/calls", json={"callee_id": caller["user_id"]}, headers=auth(caller)
    )
    assert r.status_code == 400


async def test_unknown_callee(client: httpx.AsyncClient, pair) -> None:
    caller, _ = pair
    r = await client.post(
        "/v1/calls", json={"callee_id": "does-not-exist"}, headers=auth(caller)
    )
    assert r.status_code == 404


# --- rate limits ------------------------------------------------------------


async def test_callee_side_rate_limit_blocks_ring_spam(
    client: httpx.AsyncClient, pair
) -> None:
    """Several callers hammering one callee must be stopped by the callee-side
    limit — the per-caller limit alone would let N callers through."""
    caller, callee = pair

    statuses = []
    for i in range(8):
        spammer = await make_session(f"Spammer{i}", f"SPAM{i}-A1")
        r = await client.post(
            "/v1/calls", json={"callee_id": callee["user_id"]}, headers=auth(spammer)
        )
        statuses.append(r.status_code)

    assert 429 in statuses, f"callee never rate-limited: {statuses}"


# --- auth -------------------------------------------------------------------


async def test_bad_code_is_rejected_vaguely(client: httpx.AsyncClient, pair) -> None:
    r = await client.post(
        "/v1/auth/dev-login", json={"user_code": "NOPE-000", "display_name": "X"}
    )
    assert r.status_code == 401
    assert "invalid user code" in r.text


async def test_dev_login_rate_limited(client: httpx.AsyncClient) -> None:
    statuses = [
        (
            await client.post(
                "/v1/auth/dev-login",
                json={"user_code": "NOPE-000", "display_name": "X"},
            )
        ).status_code
        for _ in range(10)
    ]
    assert 429 in statuses, statuses


async def test_contacts_excludes_self(client: httpx.AsyncClient, pair) -> None:
    caller, callee = pair
    r = await client.get("/v1/users/contacts", headers=auth(caller))
    assert r.status_code == 200
    ids = [c["id"] for c in r.json()]
    assert callee["user_id"] in ids
    assert caller["user_id"] not in ids


async def test_healthz(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True
