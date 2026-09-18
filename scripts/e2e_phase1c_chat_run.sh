#!/usr/bin/env bash
# Live E2E for Phase 1c: one real chat turn, then read the persisted run back.
set -u
HOST=http://localhost:5012
APP=04a78ef2-7fbd-48b5-abfa-a8e715723d9a
CONV=ce014362-8502-4346-8ef8-88acfb316d1a

TOKEN=$(curl -s -X POST "$HOST/api/apps/$APP/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@zhanlu.dev","password":"admin123"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin).get("access_token",""))')

if [ -z "$TOKEN" ]; then echo "LOGIN FAILED"; exit 1; fi
echo "logged in (token ${#TOKEN} chars)"

BEFORE=$(docker exec zhanlu-copy-postgres psql -U zhanlu -d zhanlu -tAc \
  "select count(*) from agent_runs where caller_context like '%$CONV%'")
echo "runs for this conversation before: $BEFORE"

echo "--- posting a message (real turn) ---"
curl -s -X POST "$HOST/api/apps/$APP/agents/conversations/v2/$CONV/messages" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"content":"Reply with exactly: harness check","role":"user"}' \
  -o /tmp/turn_resp.json -w 'http_status=%{http_code} time=%{time_total}s\n'
head -c 300 /tmp/turn_resp.json; echo

AFTER=$(docker exec zhanlu-copy-postgres psql -U zhanlu -d zhanlu -tAc \
  "select count(*) from agent_runs where caller_context like '%$CONV%'")
echo "runs for this conversation after: $AFTER"
