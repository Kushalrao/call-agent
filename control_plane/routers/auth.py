"""Dev-login mode (spec §1.1).

This beats Apple Sign-In for now: deterministic test pairs, zero Apple account
friction, works on any device or simulator instantly — and the session JWT is
the same structure production auth will issue, so swapping in Sign in with
Apple later touches only this module.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import ratelimit
from ..config import get_settings
from ..db import get_session
from ..logging_setup import Events, log_event
from ..models import User
from ..schemas import DevLoginRequest, SessionResponse
from ..security import issue_session_token

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.post("/dev-login", response_model=SessionResponse)
async def dev_login(
    body: DevLoginRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SessionResponse:
    settings = get_settings()

    if settings.auth_mode != "dev":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="dev login disabled"
        )

    client_ip = request.client.host if request.client else "unknown"
    ratelimit.check("dev_login", client_ip, settings.dev_login_per_min_per_ip)

    code = body.user_code.strip().upper()
    user = (
        await session.execute(select(User).where(User.user_code == code))
    ).scalar_one_or_none()

    if user is None:
        # Deliberately vague: do not confirm whether a code exists.
        log_event(Events.ERROR_UNAUTHORIZED, level="warn", reason="bad_code", ip=client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid user code"
        )

    # The login request IS the login event, and it may update the display name.
    if body.display_name.strip() and body.display_name.strip() != user.display_name:
        user.display_name = body.display_name.strip()
        await session.commit()

    log_event(Events.AUTH_DEV_LOGIN, user_id=user.id, display_name=user.display_name)

    return SessionResponse(
        access_token=issue_session_token(user),
        user_id=user.id,
        display_name=user.display_name,
        livekit_url=settings.livekit_url,
    )
