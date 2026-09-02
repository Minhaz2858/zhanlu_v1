#!/usr/bin/env python3
"""Live-verify the Sales Assistant workflow with the exact failing question."""
import json, sys, time
import requests

APP = "local-zhanlu-app"
WF = "d5288318-bbd1-48d6-9816-e450b90d4707"

r = requests.post(f"http://localhost:5002/api/apps/{APP}/auth/login",
                  json={"email": "admin@zhanlu.dev", "password": "admin123"}, timeout=15)
H = {"Authorization": f"Bearer {r.json()['access_token']}"}

question = "Sales figures (revenue, units, trends) for last week"
r = requests.post(f"http://localhost:5002/api/apps/{APP}/workflows/{WF}/run-async",
                  headers=H, json={"input": question}, timeout=15)
print("run-async:", r.status_code, r.text[:200])
run_id = r.json().get("run_id")
print("run_id:", run_id)

# stream until run_finished
url = f"http://localhost:5002/api/apps/{APP}/workflows/{WF}/runs/{run_id}/stream"
events = []
start = time.time()
with requests.get(url, headers=H, stream=True, timeout=240) as resp:
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data: "):
            continue
        try:
            evt = json.loads(raw[6:])
        except Exception:
            continue
        events.append(evt)
        t = evt.get("type")
        if t in ("node_step", "step"):
            pass
        if t == "run_finished":
            print("run_finished after %.1fs: %s" % (time.time() - start, json.dumps(evt, default=str)[:400]))
            break
print("elapsed: %.1fs" % (time.time() - start))

# summarize steps
steps = [e for e in events if e.get("type") in ("node_step", "step", "node_status")]
seen = {}
for e in steps:
    nid = e.get("node_id") or e.get("node") or e.get("id")
    status = e.get("status") or e.get("state")
    if nid and status:
        seen[nid] = status
print("node statuses:", json.dumps(seen, default=str))
