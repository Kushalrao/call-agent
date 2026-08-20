"""Session JWTs and the current-user dependency.

The JWT structure here is what production auth will issue too — swapping in
Sign in with Apple later touches exactly one module (spec §1.1).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import get_session
from .models import User

ALGORITHM = "HS256"
_bearer = HTTPBearer(auto_error=False)


def issue_session_token(user: User) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": user.id,
        "name": user.display_name,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=settings.jwt_ttl_hours)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_signing_key, algorithm=ALGORITHM)


def decode_session_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_signing_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=f"invalid session token: {exc}"
        ) from exc


async def _load_user(user_id: str, session: AsyncSession) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unknown user")
    return user


async def current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token"
        )
    claims = decode_session_token(creds.credentials)
    return await _load_user(claims["sub"], session)


async def user_from_token(token: str, session: AsyncSession) -> User:
    """For the WebSocket handshake, where a bearer header is awkward from iOS."""
    claims = decode_session_token(token)
    return await _load_user(claims["sub"], session)


CurrentUser = Annotated[User, Depends(current_user)]
