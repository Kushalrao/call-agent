"""The widget envelope, checked against the Swift decoder that consumes it.

The contract lives in two languages. A mismatch does not fail a test, throw an
exception, or log an error — it produces a blank screen on a phone during a live
call. So these tests read `WidgetModels.swift` directly rather than trusting that
the two sides agree.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from agent.widgets import (
    ENVELOPE_VERSION,
    TTL_STATUS_S,
    WIDGET_TOPIC,
    WidgetPublisher,
    agent_status,
    envelope,
)

SWIFT_MODELS = Path("ios/HandsFree/WidgetModels.swift")
SWIFT_CALLCENTER = Path("ios/HandsFree/CallCenter.swift")


# --- the cross-language contract -------------------------------------------


def test_envelope_version_matches_the_swift_guard():
    """Swift throws unsupportedVersion for anything else."""
    src = SWIFT_MODELS.read_text()
    m = re.search(r"header\.v\s*==\s*(\d+)", src)
    assert m, "could not find the version guard in WidgetModels.swift"
    assert int(m.group(1)) == ENVELOPE_VERSION


def test_topic_matches_what_ios_filters_on():
    """CallCenter drops data on any other topic, silently."""
    assert f'topic == "{WIDGET_TOPIC}"' in SWIFT_CALLCENTER.read_text()


def test_header_fields_are_snake_case_as_the_decoder_expects():
    """Swift uses .convertFromSnakeCase, so `widgetId` there means `widget_id`
    here. Sending camelCase decodes to nothing."""
    assert ".convertFromSnakeCase" in SWIFT_MODELS.read_text()
    e = envelope("agent_status", {"state": "thinking", "message": "hi"}, ttl_s=10)
    assert set(e) == {"v", "widget_id", "type", "ttl_s", "payload"}
    for key in e:
        assert key.islower(), f"{key} must be snake_case on the wire"


def test_agent_status_payload_matches_the_swift_struct():
    src = SWIFT_MODELS.read_text()
    struct = src[src.index("struct AgentStatus"):]
    struct = struct[: struct.index("}")]
    declared = set(re.findall(r"let (\w+):", struct))
    assert declared == set(agent_status("thinking", "one moment")["payload"]), (
        "agent_status payload does not match AgentStatus in WidgetModels.swift"
    )


def test_the_types_we_publish_are_types_swift_can_decode():
    cases = SWIFT_MODELS.read_text()
    for widget_type in ("agent_status", "flight_results"):
        assert f'case "{widget_type}"' in cases


# --- the envelope itself ---------------------------------------------------


def test_widget_ids_are_unique():
    ids = {envelope("agent_status", {}, ttl_s=1)["widget_id"] for _ in range(200)}
    assert len(ids) == 200


def test_status_toasts_expire_quickly():
    """A stale "thinking..." is worse than no toast at all."""
    assert agent_status("thinking", "x")["ttl_s"] == TTL_STATUS_S
    assert TTL_STATUS_S <= 30


def test_envelope_is_json_serializable():
    json.dumps(agent_status("searching", "checking flights"))


# --- publishing never breaks a call ---------------------------------------


class FakeRoom:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[bytes, str, bool]] = []
        room = self

        class LP:
            async def publish_data(self, data, *, topic, reliable):
                if room.fail:
                    raise RuntimeError("data channel closed")
                room.sent.append((data, topic, reliable))

        self.local_participant = LP()


def test_publish_sends_json_on_the_widget_topic_reliably():
    room = FakeRoom()
    pub = WidgetPublisher(room, "c1")
    assert asyncio.run(pub.status("thinking", "one moment")) is True
    data, topic, reliable = room.sent[0]
    assert topic == WIDGET_TOPIC and reliable is True
    decoded = json.loads(data)
    assert decoded["type"] == "agent_status"
    assert decoded["payload"]["state"] == "thinking"


def test_a_failed_publish_does_not_raise():
    """A widget that cannot reach a phone must never affect the call. The humans
    are talking to each other; the agent is an accessory to that."""
    pub = WidgetPublisher(FakeRoom(fail=True), "c1")
    assert asyncio.run(pub.status("thinking", "x")) is False
    assert pub.summary() == {"widgets_published": 0, "widgets_failed": 1}


def test_publish_counts_are_reported():
    pub = WidgetPublisher(FakeRoom(), "c1")
    for _ in range(3):
        asyncio.run(pub.status("thinking", "x"))
    assert pub.summary()["widgets_published"] == 3
