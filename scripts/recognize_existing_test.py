#!/usr/bin/env python3
"""New-conversation recognition test: user says 'I have a model, update it'
in a FRESH chat. Expect: fusion360_info / fusion360_project to recognize the
existing bolt+nut on the canvas, then in-place modification, ZERO clears."""
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
                new = tcs[len(tools):]
                for c in new:
                    tools.append(c)
                    print("[%6.1fs] [tool] %s %s" % (dt, c.get("name"), json.dumps(c.get("arguments") or c.get("args") or {}, ensure_ascii=False)[:140]), flush=True)
            elif t == "delta":
                txt = ev.get("content", "")
                if txt.strip():
                    print("[%6.1fs] delta: %.100s" % (dt, txt.replace("\n", " ")), flush=True)
            elif t == "done":
                print("[%6.1fs] DONE: %.200s" % (dt, ev.get("content", "")), flush=True)
            elif t == "error":
                print("[%6.1fs] ERROR: %s" % (dt, ev.get("message")), flush=True)
    print("TURN TOOLS: %s" % ", ".join(t.get("name") for t in tools), flush=True)
    return tools

auth = call("POST", "/auth/login", {"email": "admin@zhanlu.dev", "password": "admin123"})
TOKEN = auth["access_token"]
print("LOGIN OK", flush=True)

conv = call("POST", "/agents/conversations", {"agent_name": "CAD Agent", "title": "recognize existing model"})
conv_id = conv.get("id") or conv.get("conversation_id")
print("FRESH CONV %s" % conv_id, flush=True)
call("PUT", "/agents/conversations/%s/permission-mode" % conv_id, {"mode": "full_auto"})

stream(conv_id, "I have an existing model on the Fusion canvas — a hex bolt and a matching nut. Don't rebuild them. Add a flat washer under the nut (outer dia 12 mm, inner dia 5.5 mm, thickness 1.5 mm).")
print("\nDONE", flush=True)
