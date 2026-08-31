#!/usr/bin/env python3
"""Reproduce the user's exact request against the live backend and dump ALL SSE events."""
import json, sys, time, urllib.request

BASE = "http://localhost:5002"
APP = "local-zhanlu-app"

def post(url, body, token=None, timeout=60):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

# 1. login
tok = post(f"{BASE}/api/apps/{APP}/auth/login",
           {"email": "admin@zhanlu.dev", "password": "admin123"})["access_token"]

# 2. create fresh conversation
conv = post(f"{BASE}/api/apps/{APP}/agents/conversations",
            {"agent_name": "ecisco_bi_assistant",
             "metadata": {"name": "Debug Docx Repro", "description": "repro"}},
            token=tok)
conv_id = conv["id"] if isinstance(conv, dict) else conv[0]["id"]
print(f"CONV_ID={conv_id}", flush=True)

# 3. stream the exact user request
body = {"content": "give me top 5 customer list for last month give me in docx file"}
data = json.dumps(body).encode()
req = urllib.request.Request(f"{BASE}/api/apps/{APP}/agents/conversations/v3/{conv_id}/messages/stream",
                             data=data, method="POST")
req.add_header("Content-Type", "application/json")
req.add_header("Authorization", f"Bearer {tok}")

start = time.time()
with urllib.request.urlopen(req, timeout=300) as r:
    buf = b""
    while True:
        chunk = r.read(4096)
        if not chunk:
            break
        buf += chunk
        # split on event boundaries
        while b"\n\n" in buf:
            frame, buf = buf.split(b"\n\n", 1)
            line = frame.decode(errors="replace").strip()
            if not line.startswith("data: "):
                print(f"RAW: {line[:200]}", flush=True)
                continue
            try:
                ev = json.loads(line[6:])
            except Exception as e:
                print(f"PARSE_ERR: {line[:200]}", flush=True)
                continue
            t = ev.get("type", "?")
            summary = {}
            if "content" in ev and isinstance(ev["content"], str):
                summary["content"] = ev["content"][:160]
            if "tool_calls" in ev and isinstance(ev["tool_calls"], list):
                summary["tool_calls"] = [
                    {"name": tc.get("name"), "status": tc.get("status"),
                     "rows": len((tc.get("results") or {}).get("rows") or []) if isinstance(tc.get("results"), dict) else None}
                    for tc in ev["tool_calls"][:5]
                ]
            if "artifact" in ev:
                a = ev["artifact"]
                summary["artifact"] = {k: a.get(k) for k in ("artifact_id", "artifact_type", "title", "file_name", "filename")}
            if "fsm_tool_calls" in ev and isinstance(ev["fsm_tool_calls"], list):
                summary["fsm_tool_calls"] = [{"name": tc.get("name"), "status": tc.get("status")} for tc in ev["fsm_tool_calls"][:5]]
            if "state" in ev:
                summary["state"] = ev.get("state")
            if "conversation" in ev:
                convd = ev["conversation"]
                msgs = convd.get("messages") or []
                if msgs:
                    last = msgs[-1]
                    summary["last_msg"] = {"role": last.get("role"), "content": (last.get("content") or "")[:120],
                                           "tool_calls": len(last.get("tool_calls") or []) if last.get("tool_calls") else 0}
            print(f"[{time.time()-start:6.1f}s] {t} {json.dumps(summary, ensure_ascii=False)[:400]}", flush=True)

print(f"DONE in {time.time()-start:.1f}s", flush=True)
