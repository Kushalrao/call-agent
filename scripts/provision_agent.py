#!/usr/bin/env python
"""Configure the ElevenLabs conversational agent, reproducibly.

The agent's prompt, voice and tool wiring are product decisions, so they live in
the repo rather than in a dashboard nobody can diff. Re-running is safe: it
patches the existing agent and reuses the existing tool if one already points at
the same URL.

    FLIGHT_TOOL_URL=https://<tunnel>.trycloudflare.com \\
      .venv/bin/python scripts/provision_agent.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from control_plane.config import get_settings  # noqa: E402

API = "https://api.elevenlabs.io/v1/"

# Sarah. One of the four voices this account is allowed to use — free-tier keys
# are refused library voices with a 402 — and the calmest of them for a copilot
# that mostly says short factual things.
VOICE_ID = "EXAVITQu4vr4xnSDxMaL"
# Convai rejects flash_v2_5 for English agents ("English Agents must use turbo
# or flash v2"). flash_v2 is the low-latency English model it does accept.
TTS_MODEL = "eleven_flash_v2"

PROMPT = """You are a travel copilot on a voice call. You help one person plan a trip and
price flights out of India.

How to talk:
- You are speaking, not writing. Short sentences. No lists, no markdown, no fare
  codes. Never say an airport code out loud — say "Bangalore", never "BLR".
- One or two sentences per turn. If there is more to say, let them ask.
- Do not read prices out digit by digit. "About twelve and a half thousand" is
  better than "twelve thousand five hundred and eight".

Searching flights:
- When they name somewhere they want to fly, call search_flights. Do not wait to
  be asked — that is what you are for.
- The search takes several seconds. Say what you are doing in a few words first,
  then call it. Never say a price before the tool has returned one.
- The tool returns `say`. Base your first answer on it. You may shorten it or make
  it sound natural, but do not change any number, airline or platform.
- If the tool has no data for a place, ask which city they mean rather than
  guessing a nearby one. If it offers a few cities, read them back and let them
  pick.
- Origin defaults to Bangalore. Only pass an origin if they actually said one.

Answering follow-up questions:
The tool returns far more than the one fare in `say`. Use it — do not search
again for the same route just because they asked something else about it.

- `summary.airlines` — who flies this route, cheapest first. Answers "which
  airlines".
- `summary.direct_available`, `direct_count`, `cheapest_direct` — answers "is
  there a direct one" and "what does a nonstop cost".
- `summary.price_low_inr` and `price_high_inr` — the range. Answers "how much
  roughly" and "what is the most expensive".
- `summary.fastest` — quickest by duration, which is often not the cheapest.
- `options` — every flight found, cheapest first, with carrier, price, stops,
  duration, and the departure and arrival times.
- `options[].departs` and `arrives` — already local time at that airport and
  already worded for speech ("8:10 am"). Read them as they are; do not convert
  anything or add a timezone.
- `options[].duration_min` — total journey in minutes. Say it in hours and
  minutes, not "two hundred and ninety minutes".

Rules for these answers:
- Only what is in the data. If they ask something it does not cover — the
  stopover city, baggage, seats, meals — say you do not have it and offer what
  you do.
- The same flight can appear twice at different prices. That is two real offers
  on two sites, not a mistake.
- Search again only if the route, date or origin changes.

What you must not do:
- Never invent a price, an airline, a flight time or an availability. Every fact
  about a flight comes from the tool, or you do not say it.
- If you do not know something, say so, and offer to look at flights instead.
- Do not discuss anything other than travel and this trip.

Style: warm, quick, slightly understated. A friend who happens to be good at this
and is not making a performance of it."""

TOOL_DESCRIPTION = (
    "Search live flight prices for a one-way trip and return the cheapest fare. "
    "Call this whenever the user asks about flights or names a destination they "
    "want to fly to. It takes several seconds, so tell the user you are checking "
    "before calling it."
)


def request(method: str, path: str, key: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"xi-api-key": key, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def tool_config(url: str, secret: str) -> dict:
    return {
        "type": "webhook",
        "name": "search_flights",
        "description": TOOL_DESCRIPTION,
        # The search drives a real browser and takes 5-12s; the default timeout is
        # far shorter than that, so a working search would look like a failure.
        "response_timeout_secs": 60,
        "api_schema": {
            "url": url.rstrip("/") + "/tool/search_flights",
            "method": "POST",
            # A shared secret, because this endpoint drives Chrome on a laptop and
            # the tunnel is on the public internet.
            "request_headers": {
                "Content-Type": "application/json",
                "X-Tool-Secret": secret,
            },
            "request_body_schema": {
                "type": "object",
                "required": ["destination"],
                "description": "Route to price.",
                "properties": {
                    "destination": {
                        "type": "string",
                        "description": "Destination city as the user said it, e.g. 'Bali', 'Dubai'.",
                        "dynamic_variable": "",
                        "constant_value": "",
                    },
                    "origin": {
                        "type": "string",
                        "description": "Departure city if stated. Omit to default to Bangalore.",
                        "dynamic_variable": "",
                        "constant_value": "",
                    },
                    "depart_date": {
                        "type": "string",
                        "description": "Departure date yyyy-mm-dd if given. Omit for ten days out.",
                        "dynamic_variable": "",
                        "constant_value": "",
                    },
                },
            },
        },
    }


def main() -> int:
    settings = get_settings()
    key = settings.elevenlabs_api_key
    secret = os.environ.get("FLIGHT_TOOL_SECRET") or ""
    url = os.environ.get("FLIGHT_TOOL_URL") or ""

    if not key:
        print("ELEVENLABS_API_KEY is not set")
        return 1
    if not secret:
        print("FLIGHT_TOOL_SECRET is not set (see .env)")
        return 1
    if not url:
        print("FLIGHT_TOOL_URL is not set — pass the public tunnel URL")
        return 1

    status, agents = request("GET", "convai/agents", key)
    if status != 200 or not agents.get("agents"):
        print(f"could not list agents: {status} {json.dumps(agents)[:200]}")
        return 1
    agent_id = agents["agents"][0]["agent_id"]
    print(f"agent: {agent_id}")

    # Reuse a tool already pointing at this URL rather than piling up duplicates
    # every time the tunnel is restarted.
    status, existing = request("GET", "convai/tools", key)
    tool_id = None
    wanted = url.rstrip("/") + "/tool/search_flights"
    for entry in (existing.get("tools") or []) if status == 200 else []:
        config = entry.get("tool_config") or {}
        if config.get("name") == "search_flights":
            if ((config.get("api_schema") or {}).get("url")) == wanted:
                tool_id = entry.get("id")
                print(f"  reusing tool {tool_id}")
                break

    if tool_id is None:
        status, created = request(
            "POST", "convai/tools", key, {"tool_config": tool_config(url, secret)}
        )
        if status >= 300:
            print(f"  tool create failed: {status} {json.dumps(created)[:300]}")
            return 1
        tool_id = created.get("id") or (created.get("tool") or {}).get("id")
        print(f"  created tool {tool_id}")

    body = {
        "name": "hands-free trip copilot",
        "conversation_config": {
            "agent": {
                "first_message": "Hey — where are you thinking of going?",
                "language": "en",
                "prompt": {
                    "prompt": PROMPT,
                    "llm": "gemini-2.5-flash",
                    "temperature": 0.4,
                    "tool_ids": [tool_id],
                },
            },
            "tts": {"voice_id": VOICE_ID, "model_id": TTS_MODEL},
        },
    }
    status, updated = request("PATCH", f"convai/agents/{agent_id}", key, body)
    if status >= 300:
        print(f"  agent update failed: {status} {json.dumps(updated)[:400]}")
        return 1

    # Read it back rather than trusting the write.
    status, agent = request("GET", f"convai/agents/{agent_id}", key)
    config = agent.get("conversation_config", {})
    prompt = (config.get("agent") or {}).get("prompt") or {}
    print("  name         :", agent.get("name"))
    print("  first_message:", (config.get("agent") or {}).get("first_message"))
    print("  llm          :", prompt.get("llm"))
    print("  tool_ids     :", prompt.get("tool_ids"))
    print("  voice        :", (config.get("tts") or {}).get("voice_id"))
    print("  prompt chars :", len(prompt.get("prompt") or ""))
    print(f"\ntalk to it: https://elevenlabs.io/app/talk-to?agent_id={agent_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
