#!/usr/bin/env python3
"""Live verification v2: capture tool_progress events with tool names."""
import json
import sys
import urllib.request

BASE = "http://localhost:5002"
CONV_ID = "8ffb436e-3d64-4cb5-b08b-2c1213f78af7"

req = urllib.request.Request(
    f"{BASE}/api/apps/default-app/auth/login",
    data=json.dumps({"email": "admin@zhanlu.dev", "password": "admin123"}).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=10) as r:
    token = json.loads(r.read())["access_token"]

payload = {
    "content": "Weekly sales report for C5/C9 products — sales volume, revenue, and inventory, last 30 days",
    "role": "user",
    "agent_name": "skill_agent",
}
req = urllib.request.Request(
    f"{BASE}/api/apps/default-app/agents/conversations/v3/{CONV_ID}/messages/stream",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
)
print("--- STREAM ---")
guard_hits = 0
all_tools = []
done_content = ""
try:
    with urllib.request.urlopen(req, timeout=240) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                evt = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            t = evt.get("type")
            if t == "tool_progress":
                for tc in evt.get("tool_calls", []):
                    name = tc.get("name") or tc.get("tool_name") or "?"
                    status = tc.get("status", "")
                    results = tc.get("results") or {}
                    all_tools.append(f"{name}({status})")
                    if name == "dashboard_guard_intercept" or (isinstance(results, dict) and results.get("blocked")):
                        guard_hits += 1
                        print(f"  GUARD HIT: {json.dumps(tc)[:250]}")
                    else:
                        print(f"  tool: {name} status={status}")
            elif t == "done":
                done_content = evt.get("content", "")
            elif t == "error":
                print(f"  ERROR: {evt.get('message')}")
except Exception as e:
    print(f"STREAM FAILED: {e}")

print("--- SUMMARY ---")
print(f"guard intercept hits: {guard_hits}")
print(f"tools: {all_tools}")
print(f"done content length: {len(done_content)}")
print("final content preview:", done_content[:300].replace("\n", " | "))
