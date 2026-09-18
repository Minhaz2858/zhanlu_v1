#!/usr/bin/env python3
"""Live E2E probe for the plan_dag / llm_service array-schema fix.

Sends the exact question whose earlier turn thrashed in an ACT re-plan loop
and answered with an apology, on a fresh conversation bound to the same
agent + project scope, then reports the assistant reply verbatim.
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://localhost:5012/api/apps/default-app"
Q = sys.argv[1] if len(sys.argv) > 1 else "这个月的合同执行情况，图表输出"


def call(method, path, body=None, token=None, timeout=900):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:800]


st, tok = call("POST", "/auth/login", {"email": "admin@zhanlu.dev", "password": "admin123"})
if st != 200:
    print("LOGIN FAILED", st, tok)
    sys.exit(1)
token = tok["access_token"]
print("login ok")

st, conv = call("POST", "/agents/conversations", {
    "agent_name": "general_assistant",
    "title": Q,
    "metadata": {
        "description": "probe",
        "project_id": "d28cb0b0-fd61-453e-8967-df8953951a6d",
        "project_name": "Data Analysis",
        "project": "Data Analysis",
        "_source": "chat_page",
    },
}, token=token)
if st not in (200, 201):
    print("CONV FAILED", st, conv)
    sys.exit(1)
cid = conv.get("id") or conv.get("conversation", {}).get("id")
print("conversation:", cid)

st, res = call("POST", f"/agents/conversations/v2/{cid}/messages",
               {"role": "user", "content": Q}, token=token, timeout=1800)
print("post status:", st)
if st not in (200, 201):
    print("MESSAGE FAILED:", res)
    sys.exit(1)

msgs = res.get("messages") or res.get("conversation", {}).get("messages") or []
if isinstance(msgs, str):
    msgs = json.loads(msgs)
asst = [m for m in msgs if m.get("role") == "assistant"]
print("=" * 70)
print("CONVERSATION_ID:", cid)
print("assistant messages:", len(asst))
for m in asst[-1:]:
    c = m.get("content") or ""
    print("--- assistant content (first 900 chars) ---")
    print(c[:900])
    tc = m.get("tool_calls") or []
    print("--- tool_calls:", len(tc), [t.get("name") for t in tc][:12])
