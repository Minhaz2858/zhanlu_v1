"""Repro: rebuild conversation 8e749a1e messages and check for dangling tool_calls."""
import json, os, sys, urllib.request

BASE = "http://localhost:5002"

def api(path, token):
    req = urllib.request.Request(f"{BASE}{path}", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

# login local-zhanlu-app
req = urllib.request.Request(
    f"{BASE}/api/apps/local-zhanlu-app/auth/login",
    data=json.dumps({"email": "admin@zhanlu.dev", "password": "admin123"}).encode(),
    headers={"Content-Type": "application/json"},
)
token = json.loads(urllib.request.urlopen(req).read())["access_token"]

d = api(f"/api/apps/local-zhanlu-app/agents/conversations/8e749a1e-cd11-491d-98fb-4553e3fbddee", token)
msgs = d["messages"]

from app.routers.agents import _rebuild_v3_history_messages

sysmsg = "You are a helpful assistant."
out = _rebuild_v3_history_messages(sysmsg, msgs)

print("=== rebuilt messages ===")
for i, m in enumerate(out):
    tcs = m.get("tool_calls")
    print(f"{i}: role={m.get('role')} tcs={len(tcs) if tcs else 0} tid={str(m.get('tool_call_id'))[:14]!r} content={str(m.get('content'))[:60]!r}")

# Check for dangling: assistant with tool_calls must be followed by tool msgs covering ALL ids
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
print("\n=== DANGLING ===")
print(dangling if dangling else "NONE")
