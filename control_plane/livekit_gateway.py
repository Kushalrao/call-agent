"""Everything that talks to LiveKit lives here.

Tokens are minted server-side, never on device (spec §1.4). Room metadata
carries `call_id` so the agent worker picks it up at job start (spec §11.1).

When LiveKit is not configured the module degrades to a no-op that returns
placeholder tokens, so the control plane and its tests run without credentials.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from livekit import api

from .config import get_settings
from .logging_setup import Events, Timer, log_event

AGENT_IDENTITY = "agent:trip-copilot"


def _client() -> api.LiveKitAPI:
    s = get_settings()
    return api.LiveKitAPI(
        url=s.livekit_api_url, api_key=s.livekit_api_key, api_secret=s.livekit_api_secret
    )


def mint_human_token(
    *, room_name: str, user_id: str, display_name: str, call_id: str
) -> str:
    """Grants for a human participant (spec §1.4).

    canPublishData is False: humans do not publish data messages in v1, so the
    widget channel is agent-to-client only and a compromised client cannot
    fabricate a widget.
    """
    s = get_settings()
    if not s.livekit_configured:
        return f"dev-placeholder-token:{room_name}:{user_id}"

    with Timer() as t:
        token = (
            api.AccessToken(s.livekit_api_key, s.livekit_api_secret)
            .with_identity(user_id)
            .with_name(display_name)
            .with_ttl(timedelta(seconds=s.livekit_token_ttl_seconds))
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=False,
                )
            )
            .to_jwt()
        )
    log_event(
        Events.TOKEN_MINTED,
        call_id=call_id,
        identity=user_id,
        kind="human",
        latency_ms=t.ms,
    )
    return token


def mint_agent_token(*, room_name: str, call_id: str) -> str:
    """Same as a human, plus canPublishData and metadata kind=agent so clients
    can distinguish the agent participant (spec §1.4)."""
    s = get_settings()
    if not s.livekit_configured:
        return f"dev-placeholder-agent-token:{room_name}"

    return (
        api.AccessToken(s.livekit_api_key, s.livekit_api_secret)
        .with_identity(AGENT_IDENTITY)
        .with_name("Trip Copilot")
        .with_metadata('{"kind":"agent"}')
        .with_ttl(timedelta(seconds=s.livekit_token_ttl_seconds))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .to_jwt()
    )


async def create_room(*, room_name: str, call_id: str) -> None:
    s = get_settings()
    if not s.livekit_configured:
        log_event("livekit.skipped", call_id=call_id, reason="not_configured", op="create_room")
        return

    lk = _client()
    try:
        with Timer() as t:
            await lk.room.create_room(
                api.CreateRoomRequest(
                    name=room_name,
                    # Must comfortably exceed the 45s ring timeout, or a room
                    # could be reaped while the callee's phone is still ringing.
                    empty_timeout=300,
                    max_participants=3,  # two humans + the agent
                    metadata=f'{{"call_id":"{call_id}"}}',
                )
            )
        log_event("livekit.room_created", call_id=call_id, room=room_name, latency_ms=t.ms)
    except Exception as exc:  # noqa: BLE001 - never let LiveKit break the API
        log_event(
            Events.ERROR_LIVEKIT, level="error", call_id=call_id, op="create_room", error=str(exc)
        )
    finally:
        await lk.aclose()


async def delete_room(*, room_name: str, call_id: str) -> None:
    """Deleting the room disconnects everyone — this is how a call ends."""
    s = get_settings()
    if not s.livekit_configured:
        log_event("livekit.skipped", call_id=call_id, reason="not_configured", op="delete_room")
        return

    # A call can end before its room finished being created; deleting first
    # would fail and leave the room to leak until empty_timeout.
    await wait_for_room(call_id)

    lk = _client()
    try:
        await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))
        log_event("livekit.room_deleted", call_id=call_id, room=room_name)
    except Exception as exc:  # noqa: BLE001
        log_event(
            Events.ERROR_LIVEKIT, level="error", call_id=call_id, op="delete_room", error=str(exc)
        )
    finally:
        await lk.aclose()


async def dispatch_agent(*, room_name: str, call_id: str) -> None:
    """Explicit dispatch, fired when the call reaches ACTIVE.

    There is no consent gate (spec §3 amendment 2026-08-20). If the worker is
    not running this logs and moves on — the humans' call is unaffected, which
    is the invariant that matters (spec §10).
    """
    s = get_settings()
    if not s.dispatch_agent:
        log_event("agent.dispatch_disabled", call_id=call_id, room=room_name)
        return
    if not s.livekit_configured:
        log_event("livekit.skipped", call_id=call_id, reason="not_configured", op="dispatch")
        return

    lk = _client()
    try:
        with Timer() as t:
            await lk.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    room=room_name,
                    agent_name=s.agent_name,
                    metadata=f'{{"call_id":"{call_id}"}}',
                )
            )
        log_event(
            Events.AGENT_DISPATCHED,
            call_id=call_id,
            room=room_name,
            agent_name=s.agent_name,
            latency_ms=t.ms,
        )
    except Exception as exc:  # noqa: BLE001
        log_event(
            Events.ERROR_LIVEKIT, level="error", call_id=call_id, op="dispatch", error=str(exc)
        )
    finally:
        await lk.aclose()


def client_config() -> dict[str, Any]:
    """The only LiveKit value the iOS bundle is allowed to know (spec §9)."""
    return {"livekit_url": get_settings().livekit_url}


# --- background execution ---------------------------------------------------
#
# MEASURED 2026-08-20: LiveKit Cloud room create/delete takes 1.4-2.6s from
# Bangalore (consistent, not a cold start, unaffected by client reuse). None of
# it may sit in front of a user-visible action:
#
#   - Room creation must not delay the ring. LiveKit auto-creates a room when
#     the first participant joins, so an explicit create is only needed to set
#     metadata and limits - it can finish after the phone starts buzzing.
#   - Room deletion must not delay the "call ended" response.
#   - Agent dispatch must never block anything. The humans are already talking;
#     the agent joining a second later is invisible, and this is the same
#     invariant as the failure matrix: the agent may fail, the call may not.

_background: set[asyncio.Task[Any]] = set()

# call_id -> the in-flight create_room task.
#
# Backgrounding room creation introduced an ordering hazard: anything that acts
# on the room (set metadata, delete it) can now run before the room exists. On a
# call shorter than the ~2s create latency that means a failed delete and a
# leaked room that lingers until empty_timeout. Dependent operations wait on
# this task instead of racing it.
_room_creates: dict[str, asyncio.Task[Any]] = {}


async def wait_for_room(call_id: str, *, timeout: float = 10.0) -> None:
    """Block until this call's room creation has finished, if one is in flight."""
    task = _room_creates.get(call_id)
    if task is None or task.done():
        return
    try:
        # shield: a timeout here must not cancel the create itself, or we would
        # turn a slow create into no room at all.
        await asyncio.wait_for(asyncio.shield(task), timeout)
    except asyncio.TimeoutError:
        log_event(
            Events.ERROR_LIVEKIT,
            level="warn",
            call_id=call_id,
            op="wait_for_room",
            error=f"room creation still pending after {timeout}s",
        )


def spawn_create_room(*, room_name: str, call_id: str) -> None:
    """Create the room off the request path, tracked so others can wait on it."""
    task = asyncio.ensure_future(create_room(room_name=room_name, call_id=call_id))
    _room_creates[call_id] = task
    _background.add(task)

    def _done(t: asyncio.Task[Any]) -> None:
        _background.discard(t)
        _room_creates.pop(call_id, None)
        if not t.cancelled() and t.exception() is not None:
            log_event(
                Events.ERROR_LIVEKIT,
                level="error",
                call_id=call_id,
                op="create_room",
                error=str(t.exception()),
            )

    task.add_done_callback(_done)


def fire_and_forget(coro: Any, *, label: str, call_id: str) -> None:
    """Run a slow LiveKit call off the request path.

    Keeps a strong reference so the task is not garbage collected mid-flight
    (asyncio only holds a weak reference), and logs any exception rather than
    letting it vanish into a never-awaited task.
    """
    task = asyncio.ensure_future(coro)
    _background.add(task)

    def _done(t: asyncio.Task[Any]) -> None:
        _background.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            log_event(
                Events.ERROR_LIVEKIT,
                level="error",
                call_id=call_id,
                op=label,
                error=str(exc),
            )

    task.add_done_callback(_done)


async def ensure_room_metadata(*, room_name: str, call_id: str) -> None:
    """Guarantee the room carries call_id before the agent is dispatched.

    Needed because room creation is now backgrounded: if a human joins before
    our create lands, LiveKit auto-creates the room *without* metadata, and the
    agent worker would then have no call_id to correlate its logs with. Setting
    it explicitly here is idempotent and covers both orderings.
    """
    s = get_settings()
    if not s.livekit_configured:
        return

    await wait_for_room(call_id)

    lk = _client()
    try:
        await lk.room.update_room_metadata(
            api.UpdateRoomMetadataRequest(
                room=room_name, metadata=f'{{"call_id":"{call_id}"}}'
            )
        )
        log_event("livekit.metadata_set", call_id=call_id, room=room_name)
    except Exception as exc:  # noqa: BLE001
        log_event(
            Events.ERROR_LIVEKIT,
            level="warn",
            call_id=call_id,
            op="update_room_metadata",
            error=str(exc),
        )
    finally:
        await lk.aclose()


async def agent_present(*, room_name: str) -> bool:
    """Whether an agent participant is actually in the room."""
    lk = _client()
    try:
        participants = await lk.room.list_participants(
            api.ListParticipantsRequest(room=room_name)
        )
        return any(
            '"kind":"agent"' in (p.metadata or "") or p.identity.startswith("agent-")
            for p in participants.participants
        )
    except Exception:  # noqa: BLE001
        return False
    finally:
        await lk.aclose()


async def prepare_and_dispatch(
    *, room_name: str, call_id: str, attempts: int = 3, wait_s: float = 6.0
) -> None:
    """Background chain that runs once a call goes ACTIVE.

    Dispatch is verified rather than assumed. The worker's connection to LiveKit
    drops and re-registers on an unstable network — observed twice in three
    minutes on a phone hotspot, with "No PONG received after 15 seconds". A
    dispatch issued in that window goes to a registration that has just been
    replaced, and nothing picks it up: the humans get a perfectly good call with
    no agent in it, and the only trace is an `agent.dispatched` with no matching
    `agent.joined`.

    So it is retried. A duplicate dispatch is harmless — the worker takes one job
    per room — while a lost one silently removes the entire product from the call.
    """
    await ensure_room_metadata(room_name=room_name, call_id=call_id)

    for attempt in range(1, attempts + 1):
        await dispatch_agent(room_name=room_name, call_id=call_id)
        await asyncio.sleep(wait_s)
        if await agent_present(room_name=room_name):
            if attempt > 1:
                log_event("agent.dispatch_recovered", call_id=call_id,
                          room=room_name, attempts=attempt)
            return
        log_event(
            "agent.dispatch_unconfirmed",
            level="warn",
            call_id=call_id,
            room=room_name,
            attempt=attempt,
            detail="dispatched but no agent joined — retrying"
            if attempt < attempts
            else "giving up; the call continues without the agent",
        )
