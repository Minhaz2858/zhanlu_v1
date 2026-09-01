#!/usr/bin/env python3
"""E2E regression: follow-up turn must produce a NEW artifact for a new topic.

Pre-fix: "can make a supply chain snapshot for last 30 days and give me in
html" hit the T15 strong re-export path → "Document ready." + the SAME
weekly-report artifact (conv 945c7cf2, 2026-08-29). Post-fix: the intent
router vetoes new-topic requests, the agent loop runs, and a NEW artifact
(title contains supply chain) must appear.
"""
import json
import sys
import urllib.request

BASE = "http://localhost:5002"
CONV_ID = "945c7cf2-bac7-4d1b-80b6-0de484a0f943"  # has prior weekly-report artifact

req = urllib.request.Request(
    f"{BASE}/api/apps/default-app/auth/login",
    data=json.dumps({"email": "admin@zhanlu.dev", "password": "admin123"}).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=10) as r:
    token = json.loads(r.read())["access_token"]

payload = {
    "content": "can make a supply chian sanpshoot for last 30 days and give me in html",
    "role": "user",
    "agent_name": "general_assistant",
}
req = urllib.request.Request(
    f"{BASE}/api/apps/default-app/agents/conversations/v3/{CONV_ID}/messages/stream",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
)
print("--- STREAM ---")
tool_names = []
done_content = ""
doc_ready = False
try:
    with urllib.request.urlopen(req, timeout=300) as r:
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
                    tool_names.append(f"{name}({status})")
            elif t == "done":
                done_content = evt.get("content", "")
                if "Document ready" in done_content:
                    doc_ready = True
except Exception as e:
    print(f"STREAM FAILED: {e}")

print("--- SUMMARY ---")
print(f"tools: {tool_names}")
print(f"document-ready shortcut: {doc_ready}")
print(f"done content length: {len(done_content)}")
print("content preview:", done_content[:350].replace("\n", " | "))
