from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from . import livekit_gateway
from .config import get_settings
from .db import SessionLocal, create_all
from .events_hub import hub
from .logging_setup import Events, log_event
from .models import Call, CallState
from .routers import agent, auth, calls, users
from .security import user_from_token


async def _ring_timeout_sweeper() -> None:
    """Transition RINGING calls past the timeout to TIMEOUT (spec §1.3).

    A sweeper rather than a per-call timer: it survives a process restart, which
    a scheduled task would not.
    """
    settings = get_settings()
    while True:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(
                seconds=settings.ring_timeout_seconds
            )
            async with SessionLocal() as session:
                stale = (
                    await session.execute(
                        select(Call).where(
                            Call.state == CallState.RINGING, Call.created_at < cutoff
                        )
                    )
                ).scalars().all()

                for call in stale:
                    call.state = CallState.TIMEOUT
                    call.ended_at = datetime.now(timezone.utc)
                    log_event(Events.CALL_TIMEOUT, call_id=call.id)
                await session.commit()

                for call in stale:
                    livekit_gateway.fire_and_forget(
                        livekit_gateway.delete_room(
                            room_name=call.room_name, call_id=call.id
                        ),
                        label="delete_room",
                        call_id=call.id,
                    )
                    await hub.send(
                        call.caller_id,
                        {"type": "call_timeout", "call_id": call.id},
                        call_id=call.id,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the sweeper must never die
            log_event(Events.ERROR_INTERNAL, level="error", where="sweeper", error=str(exc))

        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    await create_all()
    log_event(
        "service.started",
        auth_mode=settings.auth_mode,
        livekit_configured=settings.livekit_configured,
        agent_dispatch=settings.dispatch_agent,
    )

    sweeper = asyncio.create_task(_ring_timeout_sweeper())
    try:
        yield
    finally:
        sweeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweeper
        log_event("service.stopped")


app = FastAPI(title="hands-free control plane", version="0.1.0", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(calls.router)
app.include_router(agent.router)


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    settings = get_settings()
    return {
        "ok": True,
        "auth_mode": settings.auth_mode,
        "livekit_configured": settings.livekit_configured,
        "agent_dispatch": settings.dispatch_agent,
    }


@app.websocket("/v1/events")
async def events_socket(
    websocket: WebSocket, token: str = Query(..., description="session JWT")
) -> None:
    """Dev-mode ringing transport (spec §2.2b).

    Auth via query param rather than a header: WebSocket headers are awkward
    from URLSession on iOS, and the token is short-lived and TLS-protected.
    """
    async with SessionLocal() as session:
        try:
            user = await user_from_token(token, session)
        except Exception:  # noqa: BLE001
            await websocket.close(code=4401)
            log_event(Events.ERROR_UNAUTHORIZED, level="warn", where="ws_handshake")
            return

    await websocket.accept()
    await hub.register(user.id, websocket)
    try:
        await websocket.send_json({"type": "hello", "user_id": user.id})
        while True:
            # The client sends nothing meaningful; this read is how we notice a
            # disconnect. Ping frames are handled by uvicorn beneath us.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log_event(Events.ERROR_INTERNAL, level="warn", where="ws_loop", error=str(exc))
    finally:
        await hub.unregister(user.id, websocket)
