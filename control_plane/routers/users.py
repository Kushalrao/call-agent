from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..events_hub import hub
from ..models import User
from ..schemas import ContactOut, VoipTokenRequest
from ..security import CurrentUser

router = APIRouter(prefix="/v1", tags=["users"])


@router.get("/users/contacts", response_model=list[ContactOut])
async def contacts(
    me: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ContactOut]:
    """v1: everyone in the seeded list.

    SECURITY: this is a directory-scrape surface and must not survive past the
    seed list. Before any real signup flow, gate it behind an explicit social
    graph (spec §1.2 authorization note).
    """
    rows = (
        await session.execute(select(User).where(User.id != me.id).order_by(User.display_name))
    ).scalars()
    return [
        ContactOut(
            id=u.id,
            display_name=u.display_name,
            avatar_url=u.avatar_url,
            online=hub.is_online(u.id),
        )
        for u in rows
    ]


@router.post("/devices/voip-token", status_code=204)
async def register_voip_token(
    body: VoipTokenRequest,
    me: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Stored now, used in Phase 7 when WebSocket ringing is swapped for
    PushKit. Registering early means the transport swap needs no client change
    beyond the push handler itself."""
    me.voip_push_token = body.voip_push_token
    session.add(me)
    await session.commit()
