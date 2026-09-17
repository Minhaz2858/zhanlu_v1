#!/usr/bin/env bash
# E2E: a connected MCP server's tool must be visible to the agent AND callable.
#
#   scripts/e2e/mcp_agent_tools_e2e.sh [BASE_URL] [CONTAINER]
#
# Proves, in order:
#   1. the server is connected and its tools are published to the tool registry
#   2. the running process's registry holds the namespaced tool names
#   3. a real LLM turn calls mcp__<server>__<tool> and gets the server's answer back
#
# Requires: a running stack, and scripts/e2e/fake_mcp_server.py copied into the
# backend container (see CONTAINER below). Cleans up after itself unless
# KEEP=1.
#
# IMPORTANT: the copy stack must run with LLM_ENABLE_THINKING=false. With
# thinking mode on, the SECOND LLM request of any tool turn is rejected by
# DeepSeek with "The `reasoning_content` in the thinking mode must be passed
# back to the API" — the tool still runs, but the turn cannot finish, so the
# final answer is unobservable.
set -uo pipefail

BASE="${1:-http://localhost:5012}"
CONTAINER="${2:-zhanlu-copy-backend}"
APP_ID="default"
SERVER_NAME="e2e-demo"
MARKER="SUM_MARKER:42"

pass() { printf '\033[32m  PASS\033[0m %s\n' "$1"; }
fail() { printf '\033[31m  FAIL\033[0m %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
FAILURES=0

dex() { docker exec "$CONTAINER" sh -lc "$1"; }

TOKEN=$(curl -s -X POST "$BASE/api/apps/$APP_ID/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@zhanlu.dev","password":"admin123"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))')
[[ -n "$TOKEN" ]] && pass "login" || { fail "login"; exit 1; }

echo
echo "=== 1. connect the MCP server ==="
# Wipe any previous row so the run is repeatable.
PREV=$(curl -s "$BASE/api/mcp/servers" -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import sys,json;print(next((s['id'] for s in json.load(sys.stdin) if s['name']=='$SERVER_NAME'),''))")
if [[ -n "$PREV" ]]; then
  curl -s -X DELETE "$BASE/api/mcp/servers/$PREV" -H "Authorization: Bearer $TOKEN" >/dev/null
  sleep 1
fi

SID=$(curl -s -X POST "$BASE/api/mcp/servers/connect" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"name\":\"$SERVER_NAME\",\"transport\":\"stdio\",\"command\":\"python\",\"args\":[\"/tmp/fake_mcp_server.py\"],\"description\":\"E2E fake server\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))')
[[ -n "$SID" ]] && pass "connect ($SID)" || { fail "connect"; exit 1; }

sleep 3
STATUS=$(curl -s "$BASE/api/mcp/servers" -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import sys,json;print(next((s['status'] for s in json.load(sys.stdin) if s['name']=='$SERVER_NAME'),'missing'))")
[[ "$STATUS" == "connected" ]] && pass "server status=connected" || fail "server status=$STATUS"

echo
echo "=== 2. tools published to the registry ==="
PUBLISHED=$(curl -s "$BASE/api/mcp/tools" \
  | python3 -c "import sys,json;print(','.join(t['name'] for t in json.load(sys.stdin)['tools'] if t['name'].startswith('mcp__')) or 'NONE')")
if [[ "$PUBLISHED" == *"mcp__e2e_demo__add"* && "$PUBLISHED" == *"mcp__e2e_demo__echo"* ]]; then
  pass "registry has $PUBLISHED"
else
  fail "registry missing MCP tools (got: $PUBLISHED)"
fi

echo
echo "=== 3. a real LLM turn calls the MCP tool ==="
CONV=$(curl -s -X POST "$BASE/api/apps/$APP_ID/agents/conversations" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"agent_name\":\"assistant\",\"title\":\"MCP E2E\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))')

# NOTE: the v2 endpoint takes {"content": ...}; {"message": ...} is silently
# accepted as an EMPTY user message (scripts/smoke_e2e.sh still sends "message").
curl -s -X POST "$BASE/api/apps/$APP_ID/agents/conversations/v2/$CONV/messages" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"role\":\"user\",\"content\":\"Call the tool mcp__e2e_demo__add with a=17 and b=25, then reply with exactly the text the tool returned.\"}" \
  --max-time 240 > /tmp/mcp_e2e_reply.json

docker cp "$CONTAINER:/tmp/mcp_e2e_reply.json" /tmp/mcp_e2e_reply.json 2>/dev/null || true
python3 - <<PY
import json
d = json.load(open("/tmp/mcp_e2e_reply.json"))
called = answered = False
for m in d.get("messages", []):
    print(f"  --- {m.get('role')}: {(m.get('content') or '')[:300]}")
    for tc in (m.get("tool_calls") or []):
        print(f"      TOOLCALL {tc.get('name')} args={tc.get('arguments_string')} status={tc.get('status')}")
        print(f"      RESULT   {str(tc.get('results'))[:200]}")
        if tc.get("name") == "mcp__e2e_demo__add":
            called = True
    if m.get("role") == "assistant" and "$MARKER" in (m.get("content") or ""):
        answered = True
print()
print(("PASS" if called else "FAIL"), "agent called mcp__e2e_demo__add")
print(("PASS" if answered else "FAIL"), "final answer carries the MCP server's marker ($MARKER)")
PY

echo
echo "=== 4. cleanup ==="
if [[ "${KEEP:-0}" == "1" ]]; then
  echo "  KEEP=1 — left server $SID and conversation $CONV in place"
else
  curl -s -X DELETE "$BASE/api/mcp/servers/$SID" -H "Authorization: Bearer $TOKEN" >/dev/null
  # Withdrawn from the registry on delete, so no dangling tools.
  still=$(curl -s "$BASE/api/mcp/tools" \
    | python3 -c "import sys,json;print(','.join(t['name'] for t in json.load(sys.stdin)['tools'] if t['name'].startswith('mcp__e2e_demo__')) or 'none')")
  [[ "$still" == "none" ]] && pass "disconnect withdrew its tools from the registry" \
                           || fail "tools still published after disconnect: $still"
  echo "  (conversation $CONV left for inspection)"
fi

echo
[[ "$FAILURES" == "0" ]] && echo "ALL CHECKS PASSED" || echo "$FAILURES CHECK(S) FAILED"
exit "$FAILURES"
