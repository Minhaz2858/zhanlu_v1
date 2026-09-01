#!/usr/bin/env python3
"""Live 2-turn CAD follow-up test: does an 'update' request modify the SAME model
or clear + rebuild? Watches turn 2's tool stream for fusion360_clear.

Usage: python3 followup_update_test.py
Env overrides: ZHLU_BASE (default http://localhost:5002), ZHLU_APP (default zhanlu),
ZHLU_EMAIL / ZHLU_PASSWORD (default admin@zhanlu.dev / admin123).
"""

import json
import os
import sys
import urllib.parse
import urllib.request

BASE = os.environ.get("ZHLU_BASE", "http://localhost:5002") + "/api/apps/" + os.environ.get("ZHLU_APP", "zhanlu")
EMAIL = os.environ.get("ZHLU_EMAIL", "admin@zhanlu.dev")
PASSWORD = os.environ.get("ZHLU_PASSWORD", "admin123")
AGENT_NAME = os.environ.get("ZHLU_AGENT", "CAD Agent")


def call(method, path, body=None, token=None, stream=False, timeout=600):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if stream:
        return urllib.request.urlopen(req, timeout=timeout)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def stream_turn(conv_id, token, message):
    """Post a message, return (tool_calls, final_content, error)."""
    print("\n=== TURN: %r ===" % message)
    resp = call("POST", "/agents/conversations/v3/%s/messages/stream" % conv_id,
                {"role": "user", "content": message}, token=token, stream=True)
    tools = []
    final = None
    err = None
    buf = b""
    while True:
        chunk = resp.read(8192)
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
            if t == "tool_progress":
                for c in (ev.get("tool_calls") or []):
                    name = c.get("name")
                    args = c.get("arguments") or c.get("args") or {}
                    try:
                        args = json.loads(args) if isinstance(args, str) else args
                    except Exception:
                        pass
                    tools.append((name, args))
                    print("    [tool] %s %s" % (name, json.dumps(args, ensure_ascii=False)[:220]))
            elif t == "delta":
                txt = ev.get("content", "")
                if txt:
                    print("    [delta] %s" % txt[:180].replace("\n", " "))
            elif t == "done":
                final = ev.get("content") or ev.get("final_content") or ""
                print("[stream done]")
            elif t == "error":
                err = ev.get("message")
                print("[stream error] %s" % err)
            elif t == "paused":
                print("[stream paused] waiting for approval")
    return tools, final, err


def main():
    auth = call("POST", "/auth/login", {"email": EMAIL, "password": PASSWORD})
    token = auth["access_token"]
    print("[1] LOGIN OK user=%s" % auth["user"].get("email"))

    q = urllib.parse.quote(json.dumps({"name": AGENT_NAME}))
    agents = call("GET", "/entities/AgentApp?q=%s&limit=50" % q, token=token)
    items = agents if isinstance(agents, list) else agents.get("items", [])
    if not items:
        print("[2] FAIL - agent not found: %s" % AGENT_NAME)
        return 1
    agent = items[0]
    n_tools = len((agent.get("tool_config") or {}).get("enabled_tools") or [])
    print("[2] AGENT id=%s status=%s tools=%d max_iterations=%s" % (
        agent["id"], agent.get("status"), n_tools, agent.get("max_iterations")))

    conv = call("POST", "/agents/conversations", {"agent_name": AGENT_NAME, "title": "followup update test"}, token=token)
    conv_id = conv.get("id") or conv.get("conversation_id")
    print("[3] CONVERSATION id=%s" % conv_id)

    call("PUT", "/agents/conversations/%s/permission-mode" % conv_id, {"mode": "full_auto"}, token=token)
    print("[4] PERMISSION MODE full_auto")

    # Turn 1: build the two-part model
    t1_tools, t1_final, t1_err = stream_turn(
        conv_id, token,
        "Design an M5 hex-head bolt and a matching M5 nut (M5 thread, 6 mm hex head, across-flats 8 mm). Build both parts.",
    )

    # Turn 2: INCREMENTAL UPDATE to the same model
    t2_tools, t2_final, t2_err = stream_turn(
        conv_id, token,
        "Update the existing model: make the bolt's hex head 3 mm taller, and add a 45 degree chamfer to the top edge of the bolt head. Keep the nut as is.",
    )

    print("\n==================== SUMMARY ====================")
    print("TURN 1 tools: %s" % ", ".join(n for n, _ in t1_tools))
    clears1 = sum(1 for n, _ in t1_tools if n == "fusion360_clear")
    print("TURN 1 clear count: %d" % clears1)
    print("TURN 2 tools: %s" % ", ".join(n for n, _ in t2_tools))
    clears2 = sum(1 for n, _ in t2_tools if n == "fusion360_clear")
    print("TURN 2 clear count: %d" % clears2)
    if t2_err:
        print("TURN 2 ERROR: %s" % t2_err)
    if t2_final:
        print("TURN 2 FINAL:\n%s" % t2_final[:1200])

    if clears2 == 0 and t2_tools:
        print("\nVERDICT: UPDATE-IN-PLACE (no clear in turn 2) — same model modified")
    elif clears2 > 0:
        print("\nVERDICT: TURN 2 CLEARED THE MODEL — rebuild, not update (fix needed)")
    else:
        print("\nVERDICT: turn 2 ran no tools (query-ish or error) — inspect output")
    return 0


if __name__ == "__main__":
    sys.exit(main())
