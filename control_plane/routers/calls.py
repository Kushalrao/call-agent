"""Call lifecycle — the server is the single source of truth for state (spec §1.3).

AUTHORIZATION IS LOAD-BEARING HERE. Every endpoint that takes a call_id checks
that the caller is actually a participant on that call. Without those checks a
call_id is a room key: anyone holding one could mint a token and join a stranger's
conversation. This is the classic bug in this shape of API — do not remove the
`_load_participant_call` guard.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from .. import livekit_gateway, ratelimit
from ..config import get_settings
from ..db import get_session
from ..events_hub import hub
from ..logging_setup import Events, log_event
from ..models import Call, CallState, User
from ..schemas import CallOut, CallStateOut, CreateCallRequest
from ..security import CurrentUser

router = APIRouter(prefix="/v1/calls", tags=["calls"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _load_participant_call(
    call_id: str, user: User, session: AsyncSession
) -> Call:
    """Load a call, or 404 if this user has no business seeing it.

    Returns 404 rather than 403 for a non-participant: a 403 would confirm the
    call_id exists.
    """
    call = await session.get(Call, call_id)
    if call is None or not call.is_participant(user.id):
        log_event(
            Events.ERROR_UNAUTHORIZED,
            level="warn",
            call_id=call_id,
            user_id=user.id,
            reason="not_a_participant",
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="call not found")
    return call


@router.post("", response_model=CallOut, status_code=201)
async def create_call(
    body: CreateCallRequest,
    me: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CallOut:
    settings = get_settings()

    # Two limits. The per-caller limit stops a runaway client; the per-callee
    # limit is what stops one user being used as a ring-spam target.
    ratelimit.check("calls_caller", me.id, settings.calls_per_min_per_caller)
    ratelimit.check("calls_callee", body.callee_id, settings.calls_per_min_per_callee)

    if body.callee_id == me.id:
        raise HTTPException(status_code=400, detail="cannot call yourself")

    callee = await session.get(User, body.callee_id)
    if callee is None:
        raise HTTPException(status_code=404, detail="callee not found")

    call = Call(caller_id=me.id, callee_id=callee.id, state=CallState.INITIATED)
    session.add(call)
    await session.commit()
    await session.refresh(call)

    log_event(
        Events.CALL_CREATED,
        call_id=call.id,
        room=call.room_name,
        caller_id=call.caller_id,
        callee_id=call.callee_id,
    )

    # Backgrounded: room create costs 1.4-2.6s against LiveKit Cloud, and none
    # of that may sit in front of the callee's phone ringing. LiveKit
    # auto-creates the room on first join, so the explicit create only needs to
    # land before the agent is dispatched, which is seconds later.
    livekit_gateway.spawn_create_room(room_name=call.room_name, call_id=call.id)

    token = livekit_gateway.mint_human_token(
        room_name=call.room_name,
        user_id=me.id,
        display_name=me.display_name,
        call_id=call.id,
    )

    call.state = CallState.RINGING
    await session.commit()
    log_event(Events.CALL_RINGING, call_id=call.id)

    # Dev transport. The payload mirrors what the PushKit VoIP push will carry,
    # so the client's CallKit path is identical in Phase 7.
    delivered = await hub.send(
        callee.id,
        {
            "type": "incoming_call",
            "call_id": call.id,
            "caller_id": me.id,
            "caller_name": me.display_name,
            "room": call.room_name,
        },
        call_id=call.id,
    )
    if delivered == 0:
        # Not an error in dev — the callee simply isn't foregrounded. This is
        # precisely the gap PushKit closes. Returned to the caller as well as
        # logged: a warning in a log file nobody is tailing looks identical to
        # the callee ignoring the call.
        log_event("call.ring_undelivered", level="warn", call_id=call.id, callee_id=callee.id)

    return CallOut(
        call_id=call.id,
        room=call.room_name,
        state=call.state,
        caller_id=call.caller_id,
        callee_id=call.callee_id,
        lk_token=token,
        livekit_url=settings.livekit_url,
        created_at=call.created_at,
        ring_delivered=delivered > 0,
    )


@router.post("/{call_id}/accept", response_model=CallOut)
async def accept_call(
    call_id: str,
    me: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CallOut:
    call = await _load_participant_call(call_id, me, session)

    # Only the callee may accept.
    if me.id != call.callee_id:
        raise HTTPException(status_code=403, detail="only the callee can accept")
    if call.state is not CallState.RINGING:
        raise HTTPException(status_code=409, detail=f"call is {call.state.value}, not RINGING")

    token = livekit_gateway.mint_human_token(
        room_name=call.room_name,
        user_id=me.id,
        display_name=me.display_name,
        call_id=call.id,
    )

    call.state = CallState.CONNECTING
    await session.commit()
    log_event(Events.CALL_ACCEPTED, call_id=call.id, by=me.id)

    await hub.send(
        call.caller_id,
        {"type": "call_accepted", "call_id": call.id},
        call_id=call.id,
    )

    return CallOut(
        call_id=call.id,
        room=call.room_name,
        state=call.state,
        caller_id=call.caller_id,
        callee_id=call.callee_id,
        lk_token=token,
        livekit_url=get_settings().livekit_url,
        created_at=call.created_at,
    )


@router.post("/{call_id}/joined", response_model=CallStateOut)
async def report_joined(
    call_id: str,
    me: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CallStateOut:
    """Client reports that room.connect() succeeded.

    In production this comes from LiveKit `participant_joined` webhooks; local
    dev has no public URL for a webhook to reach, so the client reports it. The
    ACTIVE transition and agent dispatch hang off *both* parties being in the
    room, which is the condition the spec actually specifies.
    """
    call = await _load_participant_call(call_id, me, session)

    if me.id == call.caller_id:
        call.caller_joined = True
    else:
        call.callee_joined = True
    log_event(Events.CALL_PARTICIPANT_JOINED, call_id=call.id, user_id=me.id)

    became_active = False
    if (
        call.caller_joined
        and call.callee_joined
        and call.state in {CallState.RINGING, CallState.CONNECTING}
    ):
        call.state = CallState.ACTIVE
        call.started_at = _now()
        became_active = True

    await session.commit()

    if became_active:
        log_event(Events.CALL_ACTIVE, call_id=call.id)
        # No consent gate: the agent joins every call (spec §3 amendment).
        # Backgrounded, and deliberately so: the humans are already talking, and
        # the call must never wait on the agent. ensure_room_metadata runs first
        # so the worker has a call_id even if a human's join auto-created the
        # room before our explicit create landed.
        livekit_gateway.fire_and_forget(
            livekit_gateway.prepare_and_dispatch(
                room_name=call.room_name, call_id=call.id
            ),
            label="prepare_and_dispatch",
            call_id=call.id,
        )

    return CallStateOut(call_id=call.id, state=call.state)


@router.post("/{call_id}/decline", response_model=CallStateOut)
async def decline_call(
    call_id: str,
    me: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CallStateOut:
    call = await _load_participant_call(call_id, me, session)

    if me.id != call.callee_id:
        raise HTTPException(status_code=403, detail="only the callee can decline")
    if call.state.is_terminal:
        return CallStateOut(call_id=call.id, state=call.state)

    call.state = CallState.DECLINED
    call.ended_at = _now()
    await session.commit()
    log_event(Events.CALL_DECLINED, call_id=call.id, by=me.id)

    livekit_gateway.fire_and_forget(
        livekit_gateway.delete_room(room_name=call.room_name, call_id=call.id),
        label="delete_room",
        call_id=call.id,
    )
    await hub.send(
        call.caller_id, {"type": "call_declined", "call_id": call.id}, call_id=call.id
    )

    return CallStateOut(call_id=call.id, state=call.state)


@router.post("/{call_id}/end", response_model=CallStateOut)
async def end_call(
    call_id: str,
    me: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CallStateOut:
    """Either party may end. Deleting the room disconnects everyone including
    the agent, which is also how the agent's job terminates and flushes."""
    call = await _load_participant_call(call_id, me, session)

    if call.state.is_terminal:
        return CallStateOut(call_id=call.id, state=call.state)

    duration_ms = None
    if call.started_at:
        duration_ms = int((_now() - call.started_at).total_seconds() * 1000)

    call.state = CallState.ENDED
    call.ended_at = _now()
    await session.commit()
    log_event(Events.CALL_ENDED, call_id=call.id, by=me.id, duration_ms=duration_ms)

    livekit_gateway.fire_and_forget(
        livekit_gateway.delete_room(room_name=call.room_name, call_id=call.id),
        label="delete_room",
        call_id=call.id,
    )
    await hub.send(
        call.other_party(me.id),
        {"type": "call_ended", "call_id": call.id},
        call_id=call.id,
    )

    return CallStateOut(call_id=call.id, state=call.state)


@router.get("/{call_id}", response_model=CallOut)
async def get_call(
    call_id: str,
    me: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CallOut:
    """State readback. Deliberately does NOT mint a token — tokens are issued
    only by create/accept, so this cannot be used to rejoin an ended call."""
    call = await _load_participant_call(call_id, me, session)
    return CallOut(
        call_id=call.id,
        room=call.room_name,
        state=call.state,
        caller_id=call.caller_id,
        callee_id=call.callee_id,
        created_at=call.created_at,
    )


@router.post("/{call_id}/token", response_model=CallOut)
async def refresh_token(
    call_id: str,
    me: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CallOut:
    """Re-mint a join token for a participant on a live call.

    Covers the "token expiry pre-join" row of the failure matrix (spec section 10):
    the join window is only 120s, so a client that was slow to connect refetches
    rather than failing the call. Refuses on a terminal call, so an expired token
    can never be traded for access to a finished conversation.
    """
    call = await _load_participant_call(call_id, me, session)

    if call.state.is_terminal:
        raise HTTPException(status_code=409, detail=f"call is {call.state.value}")

    token = livekit_gateway.mint_human_token(
        room_name=call.room_name,
        user_id=me.id,
        display_name=me.display_name,
        call_id=call.id,
    )
    return CallOut(
        call_id=call.id,
        room=call.room_name,
        state=call.state,
        caller_id=call.caller_id,
        callee_id=call.callee_id,
        lk_token=token,
        livekit_url=get_settings().livekit_url,
        created_at=call.created_at,
    )
