"""Full pre-call pipeline repro for conversation 8e749a1e turn-2 400.

Runs: rebuild -> prune_tool_results_only -> sanitize_messages ->
_condense_data_agent_results -> prune_between_fsm_states, then checks
for dangling assistant tool_calls (the DeepSeek 400 class).
"""
import json, sys, urllib.request

BASE = "http://localhost:5002"

def api(path, token):
    req = urllib.request.Request(f"{BASE}{path}", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

req = urllib.request.Request(
    f"{BASE}/api/apps/local-zhanlu-app/auth/login",
    data=json.dumps({"email": "admin@zhanlu.dev", "password": "admin123"}).encode(),
    headers={"Content-Type": "application/json"},
)
token = json.loads(urllib.request.urlopen(req).read())["access_token"]

d = api(f"/api/apps/local-zhanlu-app/agents/conversations/8e749a1e-cd11-491d-98fb-4553e3fbddee", token)
msgs = d["messages"]

# Append the turn-2 user message like the handler does
msgs = list(msgs) + [{"role": "user", "content": "can make sales sanpshoot for last 30 days and give me in html"}]

from app.routers.agents import _rebuild_v3_history_messages
from app.services.compaction.pre_api_prune import prune_tool_results_only
from app.services.message_sanitization import sanitize_messages

sysmsg = "You are a helpful assistant."

out = _rebuild_v3_history_messages(sysmsg, msgs)

# Pipeline step 1: prune_tool_results_only (pre-api)
prune_tool_results_only(out, model="deepseek-chat")
# Step 2: sanitize
sanitize_messages(out)
# Step 3: condense_data_agent_results
try:
    from app.routers.agents import _condense_data_agent_results
    _condense_data_agent_results(out)
    print("condense: ran OK")
except Exception as e:
    print("condense ERROR:", e)
# Step 4: fsm pruner
from app.services.agent_loop.fsm_pruner import prune_between_fsm_states
out = prune_between_fsm_states(out, current_state="llm_call")

# Dangling check
dangling = []
for i, m in enumerate(out):
    if m.get("role") == "assistant" and m.get("tool_calls"):
        need = {tc["id"] for tc in m["tool_calls"]}
        j = i + 1
        got = set()
        while j < len(out) and out[j].get("role") == "tool":
            got.add(out[j].get("tool_call_id"))
            j += 1
        missing = need - got
        if missing:
            dangling.append((i, sorted(missing)))
print("final messages:", len(out))
print("DANGLING:", dangling if dangling else "NONE")
for i, m in enumerate(out):
    print(f"{i}: {m.get('role')} tcs={len(m.get('tool_calls') or [])} tid={str(m.get('tool_call_id'))[:14]!r} c={str(m.get('content'))[:50]!r}")
