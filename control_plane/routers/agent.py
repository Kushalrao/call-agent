"""Starting a conversation with the ElevenLabs agent.

The app cannot mint these itself, and that is the whole point of this router: the
ElevenLabs API key stays on the server. A key shipped inside an iOS binary is a
key that has been published — anyone can pull it out of the app bundle and spend
the account. So the client asks us, we ask ElevenLabs, and the client only ever
sees a short-lived room token scoped to one conversation.

ElevenLabs' agent transport is LiveKit, which is why the client needs nothing new
to connect: same SDK, different host.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, HTTPException, status

from .. import ratelimit
from ..config import get_settings
from ..logging_setup import Events, Timer, log_event
from ..schemas import AgentSessionOut
from ..security import CurrentUser

router = APIRouter(prefix="/v1/agent", tags=["agent"])

ELEVENLABS_API = "https://api.elevenlabs.io/v1/"
# ElevenLabs runs its agent rooms on its own LiveKit deployment.
ELEVENLABS_LIVEKIT_URL = "wss://livekit.rtc.elevenlabs.io"
# Conversations are minted per tap, so this is really "how many times can one
# person start talking to the agent per minute".
SESSIONS_PER_MIN = 6


@router.post("/session", response_model=AgentSessionOut, status_code=201)
async def create_agent_session(me: CurrentUser) -> AgentSessionOut:
    """Mint a room token for one conversation with the agent."""
    settings = get_settings()
    ratelimit.check("agent_session", me.id, SESSIONS_PER_MIN)

    if not settings.elevenlabs_api_key or not settings.elevenlabs_agent_id:
        log_event(
            Events.ERROR_INTERNAL,
            level="error",
            reason="agent_not_configured",
            detail="ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID must both be set",
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "The voice agent is not configured"
        )

    query = urllib.parse.urlencode({"agent_id": settings.elevenlabs_agent_id})
    request = urllib.request.Request(
        f"{ELEVENLABS_API}convai/conversation/token?{query}",
        headers={"xi-api-key": settings.elevenlabs_api_key},
    )

    try:
        with Timer() as timer:
            # Blocking, but a single sub-second call on a tap. Threading it would
            # add moving parts to something the user is already waiting on.
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200].decode(errors="replace")
        log_event(
            Events.ERROR_INTERNAL,
            level="error",
            reason="elevenlabs_token_failed",
            http_status=exc.code,
            error=detail,
        )
        # Never surface their error text to the client; it can carry account
        # details, and there is nothing the app can do with it either way.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Couldn't start the agent"
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log_event(
            Events.ERROR_INTERNAL, level="error",
            reason="elevenlabs_unreachable", error=str(exc),
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Couldn't reach the agent"
        ) from exc

    token = payload.get("token")
    if not token:
        log_event(Events.ERROR_INTERNAL, level="error",
                  reason="elevenlabs_token_missing")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't start the agent")

    conversation_id = payload.get("conversation_id") or ""
    log_event(
        "agent.session_created",
        user_id=me.id,
        conversation_id=conversation_id,
        latency_ms=timer.ms,
    )

    return AgentSessionOut(
        token=token,
        livekit_url=ELEVENLABS_LIVEKIT_URL,
        conversation_id=conversation_id,
    )
