#!/usr/bin/env bash
# Start everything needed for a two-phone test, and print what to type on them.
#
# Turn on Personal Hotspot first and join both phones to it — the office Wi-Fi
# has client isolation, so the phones cannot reach this Mac on it (see
# docs/EXECUTION_PLAN.md).
set -uo pipefail
cd "$(dirname "$0")/.."

mkdir -p logs
PORT="${PORT:-8000}"

# The hotspot interface is usually bridge100; fall back to whatever has a v4 IP.
IP=$(ipconfig getifaddr bridge100 2>/dev/null \
  || ipconfig getifaddr en0 2>/dev/null \
  || echo "")

if [ -z "$IP" ]; then
  echo "No IP found. Turn on Personal Hotspot (Settings > General > Sharing) and"
  echo "join both phones to it, then re-run."
  exit 1
fi

# Chrome first, and deliberately not as a child of anything we later kill: a
# restarted browser has a cold session, and a cold session is what makes the
# flight aggregators time out.
# A stale control plane holding DISPATCH_AGENT=false is a silent failure: the
# call works perfectly and the agent simply never joins. It logs
# agent.dispatch_disabled and nothing else, which nobody is watching for.
.venv/bin/python -c "
import sys
from control_plane.config import get_settings
s = get_settings()
problems = []
if not s.dispatch_agent:
    problems.append('DISPATCH_AGENT is false — the agent will never join the call')
if not s.stt_configured:
    problems.append('DEEPGRAM_API_KEY missing — the agent cannot hear')
if not s.anthropic_api_key:
    problems.append('ANTHROPIC_API_KEY missing — no classifier')
if not s.elevenlabs_api_key:
    problems.append('ELEVENLABS_API_KEY missing — the agent cannot speak')
for p in problems:
    print('  CONFIG: ' + p)
sys.exit(1 if problems else 0)
" || { echo; echo "Fix .env and re-run. Note that a control plane started before";        echo "a .env change keeps the old value until restarted."; exit 1; }

echo "Warming Chrome for flight search ..."
.venv/bin/python -c "
from flight_scout.capture import ensure_chrome, chrome_alive
from flight_scout.watermelon import CHROME_PORT, PROFILE_DIR
ensure_chrome(port=CHROME_PORT, profile_dir=PROFILE_DIR)
print('  chrome ready on', CHROME_PORT, '->', chrome_alive(CHROME_PORT))
" || echo "  Chrome failed to start — flight search will not work"

echo "Starting control plane on ${IP}:${PORT} ..."
.venv/bin/uvicorn control_plane.main:app --host 0.0.0.0 --port "$PORT" \
  > logs/control_plane.out 2>&1 &
API_PID=$!

echo "Starting agent worker ..."
LOG_SERVICE=agent-worker LOG_TRANSCRIPTS=true LOG_PRETTY=true \
  .venv/bin/python -m agent.worker dev > logs/worker.out 2>&1 &
WORKER_PID=$!

cleanup() { kill "$API_PID" "$WORKER_PID" 2>/dev/null; }
trap cleanup EXIT INT TERM

for _ in $(seq 1 40); do
  grep -q "registered worker" logs/worker.out && break
  sleep 1
done

if ! grep -q "registered worker" logs/worker.out; then
  echo "Worker failed to register. Last lines:"
  tail -15 logs/worker.out
  exit 1
fi

cat <<TXT

  ready.

  Server URL on both phones :  http://${IP}:${PORT}
  Phone 1                   :  Kushal / KUSHAL-W2T
  Phone 2                   :  Rohan  / ROHAN-13E

  Then: call, accept, and say "hey copilot, find us flights to Bali".
  Expect a toast on BOTH phones. No voice yet.

  Live agent log below. Ctrl-C stops everything.

TXT

tail -f logs/worker.out | grep --line-buffered -E \
  "trigger\.|widget\.|aggregator\.utterance|stt\.interim|classifier\.result|llm\.|agent\.joined|ERROR"
