from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

# Configure before importing anything that reads settings.
_TMP = tempfile.mkdtemp(prefix="handsfree-test-")
os.environ.update(
    {
        "AUTH_MODE": "dev",
        "JWT_SIGNING_KEY": "test-key",
        "DATABASE_URL": f"sqlite+aiosqlite:///{_TMP}/test.db",
        "LIVEKIT_URL": "",
        "LIVEKIT_API_KEY": "",
        "LIVEKIT_API_SECRET": "",
        "DISPATCH_AGENT": "false",
        "LOG_DIR": f"{_TMP}/logs",
        "LOG_PRETTY": "false",
    }
)

import httpx  # noqa: E402
from asgi_lifespan import LifespanManager  # noqa: E402

from control_plane import ratelimit  # noqa: E402
from control_plane.db import SessionLocal, create_all, engine  # noqa: E402
from control_plane.main import app  # noqa: E402
from control_plane.models import AuthKind, Base, User  # noqa: E402
from control_plane.security import issue_session_token  # noqa: E402


@pytest.fixture(autouse=True)
async def _clean_db() -> AsyncIterator[None]:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    ratelimit.reset()
    yield


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            yield c


async def make_user(display_name: str, code: str) -> User:
    async with SessionLocal() as session:
        user = User(display_name=display_name, user_code=code, auth_kind=AuthKind.dev)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def login(client: httpx.AsyncClient, code: str, name: str) -> dict:
    r = await client.post(
        "/v1/auth/dev-login", json={"user_code": code, "display_name": name}
    )
    assert r.status_code == 200, r.text
    return r.json()


def auth(session: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['access_token']}"}


async def make_session(display_name: str, code: str) -> dict:
    """Create a user and mint a session without going through /dev-login.

    Used by tests that need many distinct users: /dev-login is rate limited per
    IP, and in a test every request shares one IP, so logging in N users would
    trip the login limiter rather than whatever the test is actually about.
    """
    user = await make_user(display_name, code)
    return {
        "access_token": issue_session_token(user),
        "user_id": user.id,
        "display_name": user.display_name,
    }


@pytest.fixture
async def pair(client: httpx.AsyncClient) -> tuple[dict, dict]:
    """Two logged-in users, the way two seeded phones would be."""
    await make_user("Kushal", "KUSHAL-7F3")
    await make_user("Rohan", "ROHAN-2K9")
    a = await login(client, "KUSHAL-7F3", "Kushal")
    b = await login(client, "ROHAN-2K9", "Rohan")
    return a, b
