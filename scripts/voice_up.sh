#!/usr/bin/env bash
# Bring up the single-user ElevenLabs agent stack:
#   Chrome (flight search) -> flight_api on :8100 -> cloudflare tunnel -> agent
#
# No LiveKit, no CallKit, no worker. One person talks to the agent in a browser
# or the ElevenLabs app; the only thing running here is the flight search.
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs

SECRET=$(grep '^FLIGHT_TOOL_SECRET=' .env | cut -d= -f2-)
if [ -z "$SECRET" ]; then echo "FLIGHT_TOOL_SECRET missing from .env"; exit 1; fi

echo "Warming Chrome (a cold profile is what makes the flight sites time out)..."
.venv/bin/python -c "
from flight_scout.capture import ensure_chrome, chrome_alive
from flight_scout.watermelon import CHROME_PORT, PROFILE_DIR
ensure_chrome(port=CHROME_PORT, profile_dir=PROFILE_DIR)
print('  chrome:', 'ready' if chrome_alive(CHROME_PORT) else 'FAILED')
"

echo "Starting the flight tool on :8100 ..."
FLIGHT_TOOL_SECRET="$SECRET" nohup .venv/bin/uvicorn flight_api.main:app \
  --host 0.0.0.0 --port 8100 > logs/flight_api.out 2>&1 &
API_PID=$!
for _ in $(seq 1 20); do
  curl -s -m 2 -o /dev/null http://127.0.0.1:8100/healthz && break; sleep 1
done

echo "Opening the tunnel ..."
nohup cloudflared tunnel --url http://localhost:8100 --no-autoupdate \
  > logs/tunnel.out 2>&1 &
TUNNEL_PID=$!
URL=""
for _ in $(seq 1 40); do
  URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" logs/tunnel.out | head -1)
  [ -n "$URL" ] && break
  sleep 1
done
if [ -z "$URL" ]; then echo "tunnel failed:"; tail -10 logs/tunnel.out; exit 1; fi

cleanup() { kill "$API_PID" "$TUNNEL_PID" 2>/dev/null; }
trap cleanup EXIT INT TERM

echo "Pointing the agent at $URL ..."
FLIGHT_TOOL_SECRET="$SECRET" FLIGHT_TOOL_URL="$URL" \
  .venv/bin/python scripts/provision_agent.py || exit 1

cat <<TXT

  ready. Tunnel URL changes on every restart, which is why the agent is
  re-provisioned above rather than configured once by hand.

  Tool requests appear below as the agent calls them. Ctrl-C stops everything.

TXT
tail -f logs/flight_api.out | grep --line-buffered -E "search\.|tool\.|POST|ERROR"
