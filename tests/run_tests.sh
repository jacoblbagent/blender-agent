#!/usr/bin/env bash
# Headless verification: runs Blender against a local mock OpenRouter endpoint.
#
#   ./tests/run_tests.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${MOCK_PORT:-8899}"
LOG="/tmp/blender_agent_mock.jsonl"
RESULT="/tmp/blender_agent_result.json"
OUT="/tmp/blender_agent_test_output.txt"
TOKENS="/tmp/blender_agent_test_token"
SETUP="/tmp/blender_agent_test_setup.json"
CHATS="/tmp/blender_agent_test_chats.json"
PASTES="/tmp/blender_agent_test_pastes"
rm -rf "$LOG" "$RESULT" "$TOKENS" "$SETUP" "$CHATS" "$PASTES"

python3 "$HERE/tests/mock_openrouter.py" --port "$PORT" --scenario "$HERE/tests/scenario.json" \
  --log "$LOG" --reset-log > /tmp/blender_agent_mock.out 2>&1 &
MOCK_PID=$!
trap 'kill $MOCK_PID 2>/dev/null' EXIT

for _ in $(seq 1 40); do
  if python3 - "$PORT" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket(); s.settimeout(0.3)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
  then break; fi
  sleep 0.25
done

MOCK_URL="http://127.0.0.1:$PORT/v1" MOCK_LOG="$LOG" MOCK_RESULT="$RESULT" \
  BLENDER_AGENT_NO_MODEL_CACHE=1 BLENDER_AGENT_TOKEN_FILE="$TOKENS" \
  BLENDER_AGENT_SETUP_FILE="$SETUP" BLENDER_AGENT_CHATS_FILE="$CHATS" \
  BLENDER_AGENT_PASTE_DIR="$PASTES" \
  blender -b --python-expr "
import bpy
bpy.ops.preferences.addon_enable(module='blender_agent')
exec(open('$HERE/tests/test_agent.py').read())
" > "$OUT" 2>&1
BLENDER_RC=$?

grep -E "^(PASS|FAIL|===|RESULT|FAILED)" "$OUT" || true
echo
echo "exit=$BLENDER_RC  full output: $OUT  mock log: $LOG"
if [[ -f "$RESULT" ]]; then
  python3 - "$RESULT" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print("VERDICT: %d/%d passed" % (d["passed"], d["total"]))
if d["failures"]:
    print("failures:", ", ".join(d["failures"]))
    sys.exit(1)
PY
else
  echo "VERDICT: no result file (Blender crashed)"; exit 1
fi
