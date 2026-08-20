from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .models import CallState


class DevLoginRequest(BaseModel):
    user_code: str = Field(min_length=3, max_length=32)
    display_name: str = Field(min_length=1, max_length=120)


class SessionResponse(BaseModel):
    access_token: str
    user_id: str
    display_name: str
    livekit_url: str


class ContactOut(BaseModel):
    id: str
    display_name: str
    avatar_url: str | None = None
    online: bool


class CreateCallRequest(BaseModel):
    callee_id: str


class CallOut(BaseModel):
    call_id: str
    room: str
    state: CallState
    caller_id: str
    callee_id: str
    lk_token: str | None = None
    livekit_url: str | None = None
    created_at: datetime
    # Whether the ring actually reached the callee's device. Without PushKit the
    # callee must be foregrounded, and a ring into a disconnected socket fails
    # silently — the caller sees a ringing screen for a phone that never heard
    # anything. Surfaced so the UI can say so instead of timing out.
    ring_delivered: bool = True


class CallStateOut(BaseModel):
    call_id: str
    state: CallState


class VoipTokenRequest(BaseModel):
    voip_push_token: str = Field(min_length=8, max_length=255)
