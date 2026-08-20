"""WebSocket ringing for dev builds (spec §2.2b).

PushKit VoIP push does not work on the Simulator and needs APNs setup even on
device, so dev builds ring over a WebSocket instead. The client reports to
CallKit exactly as it would from a push, so the downstream code path is
identical and Phase 7 is a pure transport swap.

One user may hold several sockets (two devices, or a stale socket mid-reconnect);
every socket for that user receives the event.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

from .logging_setup import Events, log_event


class EventHub:
    def __init__(self) -> None:
        self._sockets: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def register(self, user_id: str, ws: WebSocket) -> None:
        async with self._lock:
            self._sockets[user_id].add(ws)
        log_event(Events.WS_CONNECTED, user_id=user_id, sockets=len(self._sockets[user_id]))

    async def unregister(self, user_id: str, ws: WebSocket) -> None:
        async with self._lock:
            self._sockets[user_id].discard(ws)
            if not self._sockets[user_id]:
                del self._sockets[user_id]
        log_event(Events.WS_DISCONNECTED, user_id=user_id)

    async def send(self, user_id: str, payload: dict[str, Any], *, call_id: str | None = None) -> int:
        """Deliver to every socket the user holds. Returns the delivery count.

        A count of zero means the callee is not foregrounded — in dev that is a
        missed ring, and it is exactly the gap PushKit closes in Phase 7.
        """
        async with self._lock:
            targets = list(self._sockets.get(user_id, ()))

        delivered = 0
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(payload)
                delivered += 1
            except Exception:  # noqa: BLE001 - socket died between register and send
                dead.append(ws)

        for ws in dead:
            await self.unregister(user_id, ws)

        log_event(
            Events.WS_DELIVERED,
            call_id=call_id,
            user_id=user_id,
            type=payload.get("type"),
            delivered=delivered,
        )
        return delivered

    def is_online(self, user_id: str) -> bool:
        return bool(self._sockets.get(user_id))


hub = EventHub()
