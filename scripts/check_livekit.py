#!/usr/bin/env python
"""Verify LiveKit credentials actually work.

    .venv/bin/python scripts/check_livekit.py

Mints a token, creates a room, lists it, then deletes it. If this passes, the
whole control plane's LiveKit path is live — nothing else needs changing.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jwt
from livekit import api

from control_plane.config import get_settings
from control_plane.livekit_gateway import mint_human_token

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
ROOM = "credential-check-room"


def ok(msg: str) -> None:
    print(f"  {GREEN}PASS{RESET} {msg}")


def bad(msg: str, hint: str = "") -> None:
    print(f"  {RED}FAIL{RESET} {msg}")
    if hint:
        print(f"       {YELLOW}{hint}{RESET}")
    raise SystemExit(1)


async def main() -> None:
    s = get_settings()

    print("\nLiveKit credential check\n")

    # --- 1. configuration present ------------------------------------------
    if not s.livekit_url:
        bad("LIVEKIT_URL is not set", "Copy the project URL from the top of the LiveKit dashboard.")
    if not s.livekit_api_key or not s.livekit_api_secret:
        bad(
            "LIVEKIT_API_KEY / LIVEKIT_API_SECRET are not set",
            "Generate them on the project's settings page at cloud.livekit.io",
        )
    if not s.livekit_url.startswith(("ws://", "wss://")):
        bad(
            f"LIVEKIT_URL should start with wss:// — got {s.livekit_url!r}",
            "Use the wss:// form the dashboard shows; the server API URL is derived from it.",
        )
    ok(f"config present  url={s.livekit_url}  api_url={s.livekit_api_url}")

    # --- 2. token minting is locally valid ---------------------------------
    token = mint_human_token(
        room_name=ROOM, user_id="check-user", display_name="Check", call_id="cred-check"
    )
    if token.startswith("dev-placeholder"):
        bad("still returning placeholder tokens", "Settings did not load — is .env being read?")

    claims = jwt.decode(token, s.livekit_api_secret, algorithms=["HS256"], audience=None,
                        options={"verify_aud": False})
    grants = claims.get("video", {})
    if grants.get("room") != ROOM or not grants.get("roomJoin"):
        bad(f"token grants look wrong: {grants}")
    if grants.get("canPublishData") is not False:
        bad(
            f"canPublishData should be False for humans, got {grants.get('canPublishData')!r}",
            "Humans must not publish data messages, or a client could fabricate a widget.",
        )
    ok(f"token mints and verifies  identity={claims.get('sub')}  grants={grants}")

    # --- 3. the server API actually answers --------------------------------
    lk = api.LiveKitAPI(
        url=s.livekit_api_url, api_key=s.livekit_api_key, api_secret=s.livekit_api_secret
    )
    try:
        try:
            room = await lk.room.create_room(
                api.CreateRoomRequest(name=ROOM, empty_timeout=30, max_participants=3)
            )
        except Exception as exc:  # noqa: BLE001
            bad(
                f"create_room failed: {exc}",
                "Wrong key/secret, or the URL points at a different project.",
            )
        ok(f"room created  sid={room.sid}")

        rooms = await lk.room.list_rooms(api.ListRoomsRequest(names=[ROOM]))
        if not rooms.rooms:
            bad("room did not appear in list_rooms")
        ok(f"room visible in list_rooms  ({len(rooms.rooms)} match)")

        await lk.room.delete_room(api.DeleteRoomRequest(room=ROOM))
        ok("room deleted (this is how ending a call disconnects everyone)")
    finally:
        await lk.aclose()

    print(f"\n{GREEN}LiveKit is live.{RESET} Set DISPATCH_AGENT=true once the Phase 2 worker exists.\n")


if __name__ == "__main__":
    asyncio.run(main())
