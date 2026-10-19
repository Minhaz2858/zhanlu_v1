#!/usr/bin/env python3
"""Gate-pressure turn: force the figure-grounding gate to fire and verify
the refinement budget now TERMINATES the loop (fix of 2026-10-16).

Asks for derived percentages/ratios — figures the model computes rather
than copies from tool results — which is what tripped the grounding gate
in the user's remake-loop run (figures like 1.887 / 148.6M flagged as
'unverified' → regenerate → flag → regenerate…).
"""
import json, time
import requests

APP = "local-zhanlu-app"
BASE = f"http://localhost:5002/api/apps/{APP}"
QUESTION = (
    "Using the sales data: compute the month-over-month revenue growth rate "
    "for the last 3 complete months, the average order value per month, and "
    "each month's revenue as a ratio of the first month (indexed, e.g. 1.852). "
    "Report exact derived figures."
)

r = requests.post(f"{BASE}/auth/login",
                  json={"email": "admin@zhanlu.dev", "password": "admin123"}, timeout=15)
r.raise_for_status()
H = {"Authorization": f"Bearer {r.json()['access_token']}"}

r = requests.post(f"{BASE}/agents/conversations", headers=H,
                  json={"agent_name": "general_assistant",
                        "metadata": {"name": "remake-loop-gate-pressure"}},
                  timeout=15)
r.raise_for_status()
conv_id = r.json()["id"]
print("conversation:", conv_id, "| T0:", time.strftime("%H:%M:%S"))

deltas = 0
remakes = 0
preserves = 0
first_delta = False
done = None
final_len = 0
start = time.time()

with requests.post(f"{BASE}/agents/conversations/v3/{conv_id}/messages/stream",
                   headers=H, json={"content": QUESTION, "role": "user"},
                   stream=True, timeout=900) as resp:
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data: "):
            continue
        try:
            evt = json.loads(raw[6:])
        except Exception:
            continue
        t = evt.get("type")
        if t == "delta":
            deltas += 1
            first_delta = True
        elif t == "content_preserve":
            preserves += 1
            print(f"[{time.time()-start:6.1f}s] GATE FIRED — content_preserve (Refining answer…)")
        elif t == "content_replace" and first_delta:
            remakes += 1
            print(f"[{time.time()-start:6.1f}s] full remake #{remakes}")
        elif t == "done":
            done = True
            final_len = len(evt.get("content") or "")
            print(f"[{time.time()-start:6.1f}s] done (single terminal) final_len={final_len}")
            break
        elif t == "error":
            print(f"[{time.time()-start:6.1f}s] ERROR: {str(evt)[:200]}")
            break

print("\n=== GATE-PRESSURE SUMMARY ===")
print(f"deltas={deltas} content_preserve(gate firings)={preserves} full-remakes={remakes} done={done} final_len={final_len}")
print(f"conv for log grep: {conv_id}")
if done and remakes <= 1:
    print("VERDICT: PASS — loop terminated within budget, single terminal answer")
else:
    print("VERDICT: FAIL — no terminal or runaway remakes")
