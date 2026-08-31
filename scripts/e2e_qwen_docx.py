#!/usr/bin/env python3
"""Live E2E: qwen + 'give me ... docx' on the user's real Ecisco BI app."""
import json, sys, time, urllib.request

BASE = "http://localhost:5002"
APP = "14219d3d-015d-4e1d-bf2e-97c4ab6274ca"
PROJECT = "a3dc76e3-6d04-4bcf-9c14-55d1a92a4d07"

def req(method, path, body=None, token=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, resp.read()

# 1. login
st, raw = req("POST", f"/api/apps/{APP}/auth/login",
              {"email": "admin@zhanlu.dev", "password": "admin123"})
tok = json.loads(raw)["access_token"]
print("LOGIN OK", st)

# 2. create conversation
st, raw = req("POST", f"/api/apps/{APP}/agents/conversations",
              {"agent_name": "ecisco_bi_assistant",
               "metadata": {"name": "qwen-docx-e2e", "project_id": PROJECT}},
              token=tok)
conv = json.loads(raw)
cid = conv["id"]
print("CONV OK", cid)

# 3. SSE chat
body = {"content": "give me Contract Performance for last month report in docx file"}
r = urllib.request.Request(BASE + f"/api/apps/{APP}/agents/conversations/v3/{cid}/messages/stream",
                           data=json.dumps(body).encode(), method="POST")
r.add_header("Content-Type", "application/json")
r.add_header("Authorization", f"Bearer {tok}")
tool_calls = []
done = None
t0 = time.time()
with urllib.request.urlopen(r, timeout=420) as resp:
    buf = ""
    for chunk in resp:
        buf += chunk.decode(errors="replace")
        while "\n\n" in buf:
            evt, buf = buf.split("\n\n", 1)
            for line in evt.splitlines():
                if not line.startswith("data: "):
                    continue
                try:
                    j = json.loads(line[6:])
                except Exception:
                    continue
                t = j.get("type")
                if t == "tool_progress":
                    for tc in j.get("tool_calls", []):
                        nm = tc.get("name")
                        res = tc.get("results") or {}
                        ok = res.get("success") if isinstance(res, dict) else None
                        err = str(res.get("error"))[:100] if isinstance(res, dict) and res.get("error") else ""
                        aid = res.get("artifact_id") if isinstance(res, dict) else None
                        print(f"  TOOL {nm} status={tc.get('status')} success={ok} artifact={aid} err={err}", flush=True)
                        tool_calls.append((nm, tc.get("status"), ok, aid))
                elif t == "done":
                    done = j
                    print(f"  DONE content={str(j.get('content'))[:80]} elapsed={time.time()-t0:.0f}s", flush=True)
                elif t == "error":
                    print("  STREAM ERROR:", str(j.get("message"))[:300], flush=True)
                elif t == "paused":
                    print("  PAUSED (approval):", str(j.get("conversation"))[:120], flush=True)
print("ELAPSED", round(time.time() - t0, 1))
print("TOOL_CALLS:", json.dumps(tool_calls, ensure_ascii=False))
print("CONV_ID:", cid)
