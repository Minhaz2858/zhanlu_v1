#!/usr/bin/env python3
"""Run all 7 intents through the CEO Command Center v2 workflow and report
status + step statuses + output highlights."""
import json
import subprocess
import sys
import time

APP = "local-zhanlu-app"
WF = "da71da10-082e-4b35-ad1c-f7943f32ccfe"
TOKEN = open("/tmp/zl_token").read().strip()

QUESTIONS = [
    ("SALES", "what is our total sales amount?"),
    ("CUSTOMERS", "who are our top customers by revenue?"),
    ("PRODUCT", "what are our best selling products?"),
    ("INVENTORY", "how much is undelivered or in backlog?"),
    ("FINANCIAL", "what is our tax exposure and average order value?"),
    ("REPORT", "show me the weekly sales report"),
    ("GENERAL", "hello, can you help me?"),
]


def run_question(q):
    body = json.dumps({"input": q})
    t0 = time.time()
    p = subprocess.run(
        ["curl", "-s", "-X", "POST",
         f"http://localhost:5002/api/apps/{APP}/workflows/{WF}/run",
         "-H", f"Authorization: Bearer {TOKEN}",
         "-H", "Content-Type: application/json", "-d", body],
        capture_output=True, text=True, timeout=180,
    )
    dt = time.time() - t0
    try:
        d = json.loads(p.stdout)
    except Exception:
        return {"error": p.stdout[:300], "secs": round(dt, 1)}
    steps = {}
    for s in d.get("steps", []) or []:
        steps[s.get("node_id")] = s.get("status")
    out = d.get("output") or ""
    return {
        "status": d.get("status"),
        "secs": round(dt, 1),
        "steps": steps,
        "output": out[:500],
    }


def main():
    results = {}
    for intent, q in QUESTIONS:
        print(f"\n=== {intent}: {q}")
        r = run_question(q)
        results[intent] = r
        if r.get("error"):
            print("  ERROR:", r["error"])
            continue
        print("  status:", r["status"], "|", r["secs"], "s")
        print("  steps:", json.dumps(r["steps"]))
        print("  output:", r["output"].replace("\n", " | ")[:300])
    print("\n\n===== SUMMARY =====")
    for intent, r in results.items():
        if r.get("error"):
            print(f"{intent}: ERROR {r['error'][:80]}")
        else:
            n_fail = sum(1 for v in (r["steps"] or {}).values() if v in ("failed", "retrying"))
            print(f"{intent}: {r['status']} {r['secs']}s steps={len(r['steps'])} failed={n_fail}")
    with open("/tmp/ceo_v2_results.json", "w") as f:
        json.dump({k: {kk: vv for kk, vv in v.items() if kk != "output"} for k, v in results.items()}, f, indent=1)
    print("saved /tmp/ceo_v2_results.json")


if __name__ == "__main__":
    main()
