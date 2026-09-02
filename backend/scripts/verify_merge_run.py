#!/usr/bin/env python3
"""Live-verify the Merge node end-to-end against the real stack.

Creates a workflow with two parallel branches converging into a merge node,
runs it async, drains the SSE stream, and asserts:
  - every node completed (NO skipped steps)
  - the merge step's output_json.items contains BOTH branches' items in edge order

Usage:  python3 scripts/verify_merge_run.py
"""
import json
import sys
import time

import requests

APP = "local-zhanlu-app"
BASE = "http://localhost:5002"

FLOW = {
    "version": 1,
    "nodes": [
        {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 100}, "data": {"label": "Chat Trigger", "config": {}}},
        {"id": "n2", "type": "set_fields", "position": {"x": 320, "y": 0},
         "data": {"label": "Branch A", "config": {"fields": [{"name": "branch", "value": "A"}]}}},
        {"id": "n3", "type": "set_fields", "position": {"x": 320, "y": 200},
         "data": {"label": "Branch B", "config": {"fields": [{"name": "branch", "value": "B"}]}}},
        {"id": "m1", "type": "merge", "position": {"x": 640, "y": 100}, "data": {"label": "Merge", "config": {}}},
        {"id": "n4", "type": "output", "position": {"x": 960, "y": 100}, "data": {"label": "Output", "config": {}}},
    ],
    "edges": [
        {"id": "e1", "source": "n1", "target": "n2", "sourceHandle": "source-main", "targetHandle": "target-main"},
        {"id": "e2", "source": "n1", "target": "n3", "sourceHandle": "source-main", "targetHandle": "target-main"},
        {"id": "e3", "source": "n2", "target": "m1", "sourceHandle": "source-main", "targetHandle": "target-main"},
        {"id": "e4", "source": "n3", "target": "m1", "sourceHandle": "source-main", "targetHandle": "target-main-1"},
        {"id": "e5", "source": "m1", "target": "n4", "sourceHandle": "source-main", "targetHandle": "target-main"},
    ],
}


def main() -> int:
    r = requests.post(f"{BASE}/api/apps/{APP}/auth/login",
                      json={"email": "admin@zhanlu.dev", "password": "admin123"}, timeout=15)
    r.raise_for_status()
    H = {"Authorization": f"Bearer {r.json()['access_token']}"}

    # create (empty) workflow, then save the graph
    r = requests.post(f"{BASE}/api/apps/{APP}/workflows", headers=H, timeout=15)
    r.raise_for_status()
    wf_id = r.json()["id"]
    print("created workflow:", wf_id)
    try:
        r = requests.put(f"{BASE}/api/apps/{APP}/workflows/{wf_id}",
                         headers=H, json={"name": "Merge E2E Verify", "description": "temp", "flow": FLOW}, timeout=15)
        r.raise_for_status()

        r = requests.post(f"{BASE}/api/apps/{APP}/workflows/{wf_id}/run-async",
                          headers=H, json={"input": "merge me"}, timeout=15)
        r.raise_for_status()
        run_id = r.json().get("run_id")
        print("run_id:", run_id)

        url = f"{BASE}/api/apps/{APP}/workflows/{wf_id}/runs/{run_id}/stream"
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
                if evt.get("type") == "run_finished":
                    print("run_finished after %.1fs" % (time.time() - start))
                    break

        steps = [e for e in events if e.get("type") in ("node_step", "step", "node_status")]
        statuses = {}
        for e in steps:
            nid = e.get("node_id") or e.get("node") or e.get("id")
            status = e.get("status") or e.get("state")
            if nid and status:
                statuses[nid] = status
        print("node statuses:", json.dumps(statuses, default=str))

        # fetch the persisted run steps — take the LAST step per node_id
        r = requests.get(f"{BASE}/api/apps/{APP}/workflows/{wf_id}/runs", headers=H, timeout=15)
        r.raise_for_status()
        runs = r.json() if isinstance(r.json(), list) else (r.json().get("runs") or [])
        run = next((x for x in runs if str(x.get("id")) == str(run_id)), runs[0] if runs else None)
        assert run, "run not found in runs API"
        steps_api = run.get("steps") or []
        last = {}
        for s in steps_api:
            last[s["node_id"]] = s
        merged = last.get("m1") or {}
        items = (merged.get("output_json") or {}).get("items")
        print("merge step output_json.items:", json.dumps(items, default=str))

        ok = True
        for nid in ("n1", "n2", "n3", "m1", "n4"):
            if statuses.get(nid) != "completed":
                print(f"FAIL: {nid} status={statuses.get(nid)}")
                ok = False
        if items != [{"text": "merge me", "branch": "A"}, {"text": "merge me", "branch": "B"}]:
            print("FAIL: merged items mismatch — expected [A, B] in edge order")
            ok = False
        print("VERIFY:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        requests.delete(f"{BASE}/api/apps/{APP}/workflows/{wf_id}", headers=H, timeout=15)
        print("cleaned up workflow:", wf_id)


if __name__ == "__main__":
    sys.exit(main())
