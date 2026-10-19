#!/usr/bin/env python3
"""Drive one v3-stream turn against the live backend to verify the
answer-remake loop fix (2026-10-16).

Runs a fresh general_assistant conversation with a figure-heavy data
question, then summarizes the SSE stream: how many full-answer
regenerations (content_preserve / content_replace after first delta),
and whether the turn ends with exactly one final answer.
"""
import json, sys, time
import requests

APP = "local-zhanlu-app"
BASE = f"http://localhost:5002/api/apps/{APP}"
QUESTION = (
    "What are the top 10 customers by revenue for the most recent complete "
    "month? Give a ranked table with exact figures and the percentage share "
    "of each customer."
)

r = requests.post(f"{BASE}/auth/login",
                  json={"email": "admin@zhanlu.dev", "password": "admin123"}, timeout=15)
r.raise_for_status()
H = {"Authorization": f"Bearer {r.json()['access_token']}"}
print("login OK")

r = requests.post(f"{BASE}/agents/conversations", headers=H,
                  json={"agent_name": "general_assistant",
                        "metadata": {"name": "remake-loop-verify"}},
                  timeout=15)
r.raise_for_status()
conv_id = r.json()["id"]
print("conversation:", conv_id)

# mark start time so the log grep window is tight
print("T0:", time.strftime("%H:%M:%S"))

events = []
deltas = 0
replaces_after_start = 0
preserves = 0
first_delta_seen = False
final_content_len = 0
done_status = None
start = time.time()

with requests.post(f"{BASE}/agents/conversations/v3/{conv_id}/messages/stream",
                   headers=H, json={"content": QUESTION, "role": "user"},
                   stream=True, timeout=600) as resp:
    print("stream status:", resp.status_code)
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data: "):
            continue
        try:
            evt = json.loads(raw[6:])
        except Exception:
            continue
        t = evt.get("type")
        events.append(t)
        if t == "delta":
            deltas += 1
            first_delta_seen = True
        elif t == "content_preserve":
            preserves += 1
            print(f"[{time.time()-start:6.1f}s] content_preserve (refining answer…)")
        elif t == "content_replace":
            if first_delta_seen:
                replaces_after_start += 1
            print(f"[{time.time()-start:6.1f}s] content_replace (full-remake #{replaces_after_start})")
        elif t == "done":
            done_status = evt.get("completion_status")
            final_content_len = len(evt.get("content") or "")
            print(f"[{time.time()-start:6.1f}s] done status={done_status} final_len={final_content_len}")
            break
        elif t == "error":
            print(f"[{time.time()-start:6.1f}s] ERROR: {evt}")
            break

print("\n=== STREAM SUMMARY ===")
print(f"total events: {len(events)} | deltas: {deltas}")
print(f"content_preserve events: {preserves}")
print(f"full-answer remakes (content_replace after first delta): {replaces_after_start}")
print(f"done: status={done_status} final_len={final_content_len}")
print(f"conv id for log grep: {conv_id}")
print("VERDICT:", "PASS — at most one remake, single terminal answer"
      if replaces_after_start <= 1 and done_status else
      "FAIL — multiple remakes or no terminal")
