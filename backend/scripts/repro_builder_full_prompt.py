#!/usr/bin/env python3
"""Reproduce the Build-with-AI full-prompt failure against the live backend."""
import json, sys, time
import requests

BASE = "http://localhost:5002/api/apps/local-zhanlu-app"

PROMPT = (
    "Create a workflow called 'Sales Assistant' that answers sales questions. "
    "It should have: a chat trigger node; an LLM node that classifies the "
    "question into exactly one of SALES, CUSTOMERS, or GENERAL; a switch node "
    "that routes on that classification; a database query node in natural "
    "language mode on the aipdp_data_warehouse_prod database for the SALES "
    "branch that answers about revenue and order trends; a database query node "
    "in natural language mode on the same database for the CUSTOMERS branch "
    "that answers about customer revenue and top customers; an agent node "
    "using general_assistant for the GENERAL branch; an LLM node that merges "
    "the branch answer into a final response; and a markdown output node. "
    "Connect everything in order."
)

def login():
    for app in ("local-zhanlu-app", "default-app"):
        try:
            r = requests.post(f"http://localhost:5002/api/apps/{app}/auth/login",
                              json={"email": "admin@zhanlu.dev", "password": "admin123"},
                              timeout=15)
            if r.ok:
                return app, r.json()["access_token"]
        except Exception as e:
            print("login err", app, e)
    sys.exit("login failed")

app, token = login()
H = {"Authorization": f"Bearer {token}"}
print(f"logged in via {app}")

# 1. create a workflow draft
r = requests.post(f"http://localhost:5002/api/apps/{app}/workflows", headers=H, timeout=15)
print("create workflow:", r.status_code)
wf = r.json()
wf_id = wf["id"] if isinstance(wf, dict) else wf[0]["id"]
print("workflow id:", wf_id)

# 2. create workflow_builder conversation bound to the draft
r = requests.post(f"http://localhost:5002/api/apps/{app}/agents/conversations", headers=H,
                  json={"agent_name": "workflow_builder", "title": "Workflow builder",
                        "metadata": {"workflow_id": wf_id}}, timeout=15)
print("create conv:", r.status_code)
conv = r.json()
conv_id = conv["id"]
print("conv id:", conv_id, "agent_name:", conv.get("agent_name"))

# 3. stream the full prompt
url = f"http://localhost:5002/api/apps/{app}/agents/conversations/v3/{conv_id}/messages/stream"
body = {"content": PROMPT, "role": "user"}
events = []
final_content = ""
start = time.time()
try:
    with requests.post(url, headers=H, json=body, stream=True, timeout=240) as resp:
        print("stream status:", resp.status_code)
        buf = ""
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            if raw.startswith("data: "):
                data = raw[6:]
            else:
                data = raw
            try:
                evt = json.loads(data)
            except Exception:
                continue
            t = evt.get("type", "?")
            events.append(t)
            if t == "content":
                final_content += evt.get("content", "")
            if t == "flow_saved":
                print("FLOW_SAVED EVENT:", {k: evt.get(k) for k in ("status",)})
            if t == "done":
                print("done event keys:", sorted(evt.keys())[:12])
except Exception as e:
    print("stream error:", e)

print("elapsed: %.1fs" % (time.time() - start))
from collections import Counter
print("event types:", dict(Counter(events)))

# 4. inspect what got persisted
r = requests.get(f"http://localhost:5002/api/apps/{app}/workflows/{wf_id}", headers=H, timeout=15)
saved = r.json()
flow = saved.get("flow") if isinstance(saved, dict) else None
print("saved flow nodes:", len((flow or {}).get("nodes", [])) if flow else None)

print("\n===== FINAL CONTENT (first 1500 chars) =====")
print(final_content[:1500])
print("\n===== GAP DISCLOSURE PRESENT?", "Gap disclosure" in final_content)
