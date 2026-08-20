"""The agent-to-client widget channel (spec section 7).

This is the only way anything the agent decides becomes visible on a phone. The
envelope is duplicated in two languages — `ios/HandsFree/WidgetModels.swift`
decodes what this module encodes — and a mismatch is invisible until someone is
holding a phone on a live call. So the field names here are asserted against the
Swift decoder's expectations in `tests/test_widgets.py`, which reads the actual
Swift source rather than trusting a comment.

Two things the iOS decoder requires and this module must therefore honour:

- **snake_case on the wire.** Swift uses `.convertFromSnakeCase`, so `widget_id`
  becomes `widgetId` there. Sending camelCase silently fails to decode.
- **`v` must be 1.** The Swift side throws `unsupportedVersion` otherwise, and
  unknown `type` values are dropped-and-logged rather than treated as errors —
  which is what lets a newer agent ship a widget an older build never heard of.

Publishing is best-effort by design. A widget that fails to reach a phone must
never affect the call (spec section 10): the humans are talking to each other,
and the agent is an accessory to that.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from control_plane.logging_setup import Events, log_event

# Must match the topic CallCenter.swift filters on.
WIDGET_TOPIC = "widget"
ENVELOPE_VERSION = 1

# Transient status toasts expire quickly; a flight card is worth reading for a
# while. Both are enforced client-side (CallCenter tracks TTL expiry).
TTL_STATUS_S = 12
TTL_FLIGHT_RESULTS_S = 300


def envelope(widget_type: str, payload: dict[str, Any], *, ttl_s: int) -> dict[str, Any]:
    return {
        "v": ENVELOPE_VERSION,
        "widget_id": uuid.uuid4().hex[:12],
        "type": widget_type,
        "ttl_s": ttl_s,
        "payload": payload,
    }


def agent_status(state: str, message: str) -> dict[str, Any]:
    """A transient toast: "thinking" | "searching" | "error".

    Published the instant a trigger fires, before any model work starts, so the
    humans can see the agent engaging rather than wondering whether it heard
    them (spec section 5.1a). At a ~1050ms classifier and a multi-second
    reasoning turn, silence with no acknowledgement reads as broken.
    """
    return envelope("agent_status", {"state": state, "message": message},
                    ttl_s=TTL_STATUS_S)


class WidgetPublisher:
    """Publishes widgets to every participant in the room.

    Broadcast rather than targeted: the agent is a participant in a shared
    conversation, so both people see the same thing at the same time. A card only
    one party could see would make the call confusing for both.
    """

    def __init__(self, room: Any, call_id: str) -> None:
        self._room = room
        self.call_id = call_id
        self.published = 0
        self.failed = 0

    async def publish(self, widget: dict[str, Any]) -> bool:
        """Never raises. Returns whether it went out."""
        try:
            await self._room.local_participant.publish_data(
                json.dumps(widget).encode("utf-8"),
                topic=WIDGET_TOPIC,
                reliable=True,  # a dropped widget is a blank screen, not a glitch
            )
        except Exception as exc:  # noqa: BLE001
            self.failed += 1
            log_event(
                Events.ERROR_LIVEKIT,
                level="warn",
                call_id=self.call_id,
                op="publish_widget",
                widget_type=widget.get("type"),
                error=str(exc),
            )
            return False

        self.published += 1
        log_event(
            Events.WIDGET_PUBLISHED,
            call_id=self.call_id,
            widget_id=widget.get("widget_id"),
            widget_type=widget.get("type"),
            ttl_s=widget.get("ttl_s"),
            bytes=len(json.dumps(widget)),
        )
        return True

    async def status(self, state: str, message: str) -> bool:
        return await self.publish(agent_status(state, message))

    def summary(self) -> dict[str, int]:
        return {"widgets_published": self.published, "widgets_failed": self.failed}
