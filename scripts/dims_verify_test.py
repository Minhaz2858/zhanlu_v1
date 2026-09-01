#!/usr/bin/env python3
"""End-to-end: build a dims-verified part, confirm verify_build carries
expected_dimensions and the turn reports measured vs expected."""
import json, sys, time, urllib.request

BASE = "http://localhost:5002/api/apps/zhanlu"
TOKEN = None

def call(method, path, body=None, stream=False, timeout=400):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    if stream:
        return urllib.request.urlopen(req, timeout=timeout)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def stream(conv_id, msg):
    print("\n===== TURN: %s =====" % msg[:70], flush=True)
    t0 = time.time()
    resp = call("POST", "/agents/conversations/v3/%s/messages/stream" % conv_id,
                {"role": "user", "content": msg}, stream=True)
    tools = []
    buf = b""
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        buf += chunk
        while b"\n\n" in buf:
            raw, buf = buf.split(b"\n\n", 1)
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except Exception:
                continue
            t = ev.get("type")
            dt = time.time() - t0
            if t == "tool_progress":
                tcs = ev.get("tool_calls") or []
                for c in tcs[len(tools):]:
                    tools.append(c)
                    print("[%6.1fs] [tool] %s %s" % (dt, c.get("name"),
                          json.dumps(c.get("arguments") or c.get("args") or {}, ensure_ascii=False)[:170]), flush=True)
            elif t == "delta":
                txt = ev.get("content", "")
                if txt.strip():
                    print("[%6.1fs] delta: %.110s" % (dt, txt.replace("\n", " ")), flush=True)
            elif t == "done":
                print("[%6.1fs] DONE: %.240s" % (dt, ev.get("content", "")), flush=True)
            elif t == "error":
                print("[%6.1fs] ERROR: %s" % (dt, ev.get("message")), flush=True)
    print("TOOLS: %s" % ", ".join(t.get("name") for t in tools), flush=True)
    return tools

auth = call("POST", "/auth/login", {"email": "admin@zhanlu.dev", "password": "admin123"})
TOKEN = auth["access_token"]
print("LOGIN OK", flush=True)

conv = call("POST", "/agents/conversations", {"agent_name": "CAD Agent", "title": "dims verify test"})
conv_id = conv.get("id") or conv.get("conversation_id")
print("CONV %s" % conv_id, flush=True)
call("PUT", "/agents/conversations/%s/permission-mode" % conv_id, {"mode": "full_auto"})

tools = stream(conv_id, "Build a simple rectangular block: 30 mm wide, 20 mm deep, 10 mm tall. Verify the build.")

# Show the verify_build call args + result from the conversation
v = call("GET", "/agents/conversations/%s" % conv_id)
msgs = v.get("messages", [])
for m in msgs:
    for tc in (m.get("tool_calls") or []):
        if tc.get("name") == "fusion360_verify_build":
            print("\nVERIFY CALL ARGS:", json.dumps(tc.get("arguments") or tc.get("args"), ensure_ascii=False))
            res = tc.get("results") or {}
            print("VERIFY RESULT ok=%s summary=%s" % (res.get("ok"), res.get("summary")))
            print("DIM CHECKS:", json.dumps(res.get("dimension_checks"), ensure_ascii=False))
print("\nDONE", flush=True)
