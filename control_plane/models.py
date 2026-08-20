"""Schema per spec §1.1 / §1.3, as amended 2026-08-20 (no consent columns)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, UTCDateTime


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AuthKind(str, enum.Enum):
    dev = "dev"
    apple = "apple"


class CallState(str, enum.Enum):
    """INITIATED -> RINGING -> CONNECTING -> ACTIVE -> ENDED
                        \\-> DECLINED / TIMEOUT

    Note there is no AGENT_ACTIVE state: the agent joins every call, so there
    is nothing to gate (spec §1.3 amendment).
    """

    INITIATED = "INITIATED"
    RINGING = "RINGING"
    CONNECTING = "CONNECTING"
    ACTIVE = "ACTIVE"
    ENDED = "ENDED"
    DECLINED = "DECLINED"
    TIMEOUT = "TIMEOUT"

    @property
    def is_terminal(self) -> bool:
        return self in {CallState.ENDED, CallState.DECLINED, CallState.TIMEOUT}


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    auth_kind: Mapped[AuthKind] = mapped_column(
        Enum(AuthKind, native_enum=False), default=AuthKind.dev
    )
    # Short human-typeable code handed out for dev login, e.g. KUSHAL-7F3.
    user_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    apple_sub: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str] = mapped_column(String(120))
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    apns_device_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    voip_push_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    __table_args__ = (
        UniqueConstraint("user_code", name="uq_users_user_code"),
        UniqueConstraint("apple_sub", name="uq_users_apple_sub"),
    )


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Opaque: never derived from user ids, so a room name leaks nothing.
    room_name: Mapped[str] = mapped_column(String(64), default=_uuid, index=True)
    caller_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    callee_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    state: Mapped[CallState] = mapped_column(
        Enum(CallState, native_enum=False), default=CallState.INITIATED, index=True
    )

    # Which humans have actually completed room.connect(). In production this
    # comes from LiveKit participant_joined webhooks; locally there is no public
    # URL for webhooks to reach, so the client reports via POST /calls/{id}/joined
    # and the ACTIVE transition (and agent dispatch) hangs off both being true.
    caller_joined: Mapped[bool] = mapped_column(Boolean, default=False)
    callee_joined: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    def is_participant(self, user_id: str) -> bool:
        return user_id in (self.caller_id, self.callee_id)

    def other_party(self, user_id: str) -> str:
        return self.callee_id if user_id == self.caller_id else self.caller_id
