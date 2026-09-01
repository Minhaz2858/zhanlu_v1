#!/usr/bin/env python3
"""Minimal turn-2 repro: create conversation, set full_auto, build, then UPDATE.
Prints every SSE event with timestamps so we see exactly where the stream ends."""
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
    print("\n===== TURN: %s =====" % msg[:60], flush=True)
    t0 = time.time()
    resp = call("POST", "/agents/conversations/v3/%s/messages/stream" % conv_id,
                {"role": "user", "content": msg}, stream=True)
    buf = b""
    try:
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
                    print("[%6.1fs] TOOLPROGRESS %d calls, last=%s" % (dt, len(tcs), (tcs[-1] or {}).get("name")), flush=True)
                elif t == "delta":
                    print("[%6.1fs] delta: %.80s" % (dt, ev.get("content", "")), flush=True)
                else:
                    print("[%6.1fs] EVENT %s" % (dt, t), flush=True)
                    if t == "done":
                        print("  FINAL: %.200s" % ev.get("content", ""), flush=True)
                    if t == "error":
                        print("  ERR: %s" % ev.get("message"), flush=True)
        print("[%6.1fs] STREAM EOF (clean close)" % (time.time() - t0), flush=True)
    except Exception as e:
        print("[%6.1fs] STREAM EXCEPTION: %r" % (time.time() - t0, e), flush=True)

auth = call("POST", "/auth/login", {"email": "admin@zhanlu.dev", "password": "admin123"})
TOKEN = auth["access_token"]
print("LOGIN OK", flush=True)

conv = call("POST", "/agents/conversations", {"agent_name": "CAD Agent", "title": "turn2 repro"})
conv_id = conv.get("id") or conv.get("conversation_id")
print("CONV %s" % conv_id, flush=True)
call("PUT", "/agents/conversations/%s/permission-mode" % conv_id, {"mode": "full_auto"})

stream(conv_id, "Design an M5 hex-head bolt and a matching M5 nut (M5 thread, 6 mm hex head, across-flats 8 mm). Build both parts.")
stream(conv_id, "Update the existing model: make the bolt's hex head 3 mm taller, and add a 45 degree chamfer to the top edge of the bolt head. Keep the nut as is.")
print("\nDONE", flush=True)
