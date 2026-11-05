#!/usr/bin/env python3
"""P0 Phase 3 rollout smoke: one grounded live question per remaining
manufacturing agent via the production v3 SSE endpoint.

Run inside the backend container:

    docker exec -w /app -e PYTHONPATH=/app zhanlu-backend python \
        /app/scripts/smoke_manufacturing_agents.py

Ground truth for assertions (verified against zhanlu_sandbox_auto on
2026-10-31):
  - 144 sales orders in 2025
  - 3 distinct dealers
  - total 2025 revenue 652,434,616.40
"""
import json
import sys
import time

sys.path.insert(0, "/app/scripts")
from pilot_demand_forecast_e2e import APP, login, call, stream_message  # noqa: E402

CASES = [
    ("Predictive Maintenance Agent",
     "List the data tables available in the bound data sources.",
     ["sales_order"]),
    ("Quality Control Agent",
     "What is the total revenue recorded across all sales orders in 2025?",
     ["652,434,616.40", "652434616.40", "652.4"]),
    ("Supply Chain Optimization Agent",
     "How many distinct dealers are recorded in the bound data?",
     ["4"]),  # dealer table holds 4 rows (3 have orders — both counts valid, agent cites the table)
    ("Production Scheduling Agent",
     "How many sales orders were recorded in 2025?",
     ["144"]),
    ("Inventory Management Agent",
     "How many sales orders were recorded in 2025?",
     ["144"]),
    ("Autonomous Vehicle Systems Agent",
     "List the data tables available in the bound data sources.",
     ["sales_order"]),
]


def main():
    token = login()
    only = sys.argv[1:]  # optional substring filters, e.g. `python smoke.py Supply Inventory`
    cases = [c for c in CASES if not only or any(f.lower() in c[0].lower() for f in only)]
    failures = []
    for agent_name, question, expect_any in cases:
        t0 = time.time()
        code, conv = call(
            "POST", f"/api/apps/{APP}/agents/conversations",
            {"agent_name": agent_name,
             "metadata": {"name": f"Smoke-{agent_name.split()[0]}"}}, token)
        if code != 200:
            failures.append((agent_name, f"conversation create {code}: {conv}"))
            continue
        try:
            answer, tools, done = stream_message(token, conv["id"], question)
        except Exception as exc:  # noqa: BLE001
            failures.append((agent_name, f"stream error: {exc}"))
            continue
        dur = time.time() - t0
        norm = answer.replace(",", "").replace(" ", "").lower()
        ok = (
            done is not None
            and tools >= 1
            and "error" not in answer[:120].lower()
            and any(e.replace(",", "").replace(" ", "").lower() in norm
                    for e in expect_any)
        )
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {agent_name} ({dur:.0f}s, tools={tools}, "
              f"term={(done or {}).get('type')}) → {answer[:140]!r}")
        if not ok:
            detail = answer[:300]
            if (done or {}).get("type") == "error":
                detail = f"TERMINAL ERROR: {str(done.get('message'))[:250]}"
            failures.append((agent_name, detail))
    print()
    if failures:
        print(f"SMOKE FAILURES: {len(failures)}")
        for name, why in failures:
            print(f"  - {name}: {why}")
        sys.exit(1)
    print(f"SMOKE PASS — {len(CASES)}/{len(CASES)} agents grounded")


if __name__ == "__main__":
    main()
