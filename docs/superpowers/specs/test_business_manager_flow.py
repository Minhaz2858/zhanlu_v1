#!/usr/bin/env python3
"""Run the Business Manager Command Center with test inputs and assert every node status."""
import json
import sys
import urllib.request

BASE = "http://localhost:5002/api/apps/default-app"
TOKEN = open("/tmp/zhanlu_token.txt").read().strip()
WID = "b65b4dfe-89b3-4b67-b730-eac4f5801529"

# node id -> label for readable output
LABELS = {"n1": "trigger_chat", "n2": "Intent Classifier", "n3": "Route I", "n6": "Route II",
          "n4": "Sales Detail", "n5": "Sales Manager", "n7": "Customer Detail", "n8": "Customer Specialist",
          "n9": "Product Detail", "n10": "Product Specialist", "n13": "Financial Detail", "n14": "Finance Specialist",
          "n15": "Report Data", "n16": "Report Writer", "n11": "General Assistant", "n12": "KPI Snapshot",
          "n17": "Manager Synthesis", "n18": "Manager Brief"}


def run(input_text):
    body = json.dumps({"input": input_text}).encode()
    req = urllib.request.Request(BASE + f"/workflows/{WID}/run", method="POST", data=body)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=420) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode()[:2000]}
    except Exception as e:
        return {"_exc": str(e)}


def summarize(result):
    if "_http_error" in result or "_exc" in result:
        return None, result
    status = result.get("status")
    output = result.get("output")
    steps = {}
    for ev in result.get("events", []):
        if ev.get("type") == "step":
            nid = ev.get("node_id")
            st = ev.get("status")
            if nid:
                # keep LAST event per node (running -> completed)
                steps[nid] = st
    return status, {"status": status, "output": output, "steps": steps}


def main():
    tests = [
        ("SALES", "What are our sales by region?"),
        ("CUSTOMERS", "Who are our top customers by revenue?"),
        ("PRODUCTS", "Which products have the most volume?"),
        ("FINANCIAL", "How is our financial performance by region?"),
        ("REPORT", "Give me a full business overview report"),
        ("GENERAL", "Hello! How are you today?"),
    ]
    all_ok = True
    for intent, q in tests:
        print(f"\n===== {intent}: {q!r} =====")
        t0 = __import__("time").time()
        result = run(q)
        status, summ = summarize(result)
        dt = __import__("time").time() - t0
        if summ is None:
            print("  RUN ERROR:", result)
            all_ok = False
            continue
        print(f"  run status: {status}  ({dt:.0f}s)")
        for nid in ["n1", "n2", "n3", "n6", "n4", "n5", "n7", "n8", "n9", "n10", "n13", "n14", "n15", "n16", "n11", "n12", "n17", "n18"]:
            st = summ["steps"].get(nid)
            mark = "  "
            if st == "failed":
                mark = "!!"
                all_ok = False
            if st == "completed":
                mark = "OK"
            if st == "skipped":
                mark = "--"
            print(f"   [{mark}] {nid:>3} {LABELS[nid]:<22} {st or 'NO-EVENT'}")
        out = summ["output"] or ""
        print(f"  output ({len(out)} chars): {out[:400]!r}")
        if not out or "no output" in str(summ.get("status")):
            all_ok = False
    print("\n\nRESULT:", "ALL INTENTS PASS" if all_ok else "FAILURES PRESENT")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
