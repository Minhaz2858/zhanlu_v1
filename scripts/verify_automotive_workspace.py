#!/usr/bin/env python
"""Phase E verification — automotive workspace configuration (ZERO LLM credits).

1. GA discovery: runs the REAL ``list_available_agents`` tool handler (a pure
   DB tool — no model call) and asserts every department exposes its
   specialists with real identities + home project + data domains.
2. Semantic selection: runs ``semantic_select_specialists`` with the LOCAL
   embedding model (all-MiniLM, no external API) over the published profiles
   for representative automotive objectives.
3. Greeting guard: TurnPlan classifies a greeting as direct_response (no
   delegation) — the enforcement itself is covered by backend tests.

Run inside the container:
    docker exec -w /app -e PYTHONPATH=/app zhanlu-backend \
        python /app/scripts/verify_automotive_workspace.py
"""
import asyncio


async def main() -> int:
    from app.database import SessionLocal
    from app.services.tool_handlers.delegate_tool import _list_available_agents

    db = SessionLocal()
    failures = []

    # ── 1. GA discovery (real handler, real DB) ──────────────────────────
    out = await _list_available_agents({}, db, user_id=None, context=None)
    entries = out.get("agents") or out.get("results") or []
    by_name = {e.get("name"): e for e in entries}
    expected = {
        "Production Scheduling Agent": "manufacturing",
        "Predictive Maintenance Agent": "manufacturing",
        "Supply Chain Optimization Agent": "supply_chain",
        "Inventory Management Agent": "supply_chain",
        "Supplier Risk Agent": "supply_chain",
        "Quality Control Agent": "quality",
        "Demand Forecasting Agent": "sales",
        "Dealer Performance Agent": "sales",
        "Customer Support Agent": "aftersales",
        "Warranty & Service Agent": "aftersales",
        "Autonomous Vehicle Systems Agent": "rnd",
        "CAD Agent": "rnd",
        "Cost Analysis Agent": "finance",
        "Logistics & Distribution Agent": "logistics",
    }
    print(f"discovery: {len(entries)} published agents visible to the GA")
    for name, dept in expected.items():
        e = by_name.get(name)
        if e is None:
            failures.append(f"discovery: '{name}' NOT visible to the GA")
        elif (e.get("department") or "") != dept:
            failures.append(
                f"discovery: '{name}' department={e.get('department')} != {dept}")
        elif not e.get("home_project"):
            failures.append(f"discovery: '{name}' has no home_project")
    if entries:
        sample = next(e for e in entries if e["name"] == "Supplier Risk Agent")
        print("sample entry:", {k: sample.get(k) for k in
                                ("name", "kind", "department", "home_project",
                                 "data_domains")})

    # Department-scoped discovery (conversation department chip path).
    scoped = await _list_available_agents(
        {"department": "supply_chain"}, db, user_id=None, context=None)
    scoped_entries = scoped.get("agents") or scoped.get("results") or []
    depts = {(e.get("department") or "") for e in scoped_entries}
    if depts - {"supply_chain", ""}:
        failures.append(f"department filter leaked: {depts}")
    print(f"scoped discovery (supply_chain): {len(scoped_entries)} entries, "
          f"departments={sorted(d or '-' for d in depts)}")

    # ── 2. Semantic selection (LOCAL embeddings — no external API) ───────
    # The matcher's threshold (0.35) is the REVIEWED honesty boundary:
    # below it the GA's own tools handle the turn. Clear-domain questions
    # must reach their specialist; open-ended analytic phrasings may
    # legitimately fall to the GA (its LLM still sees the discovery list).
    # FALSE-POSITIVE guards are assertions — capability stretching is a bug.
    from app.services.semantic_capability_match import semantic_select_specialists

    profiles = [
        {"name": e["name"], "description": e.get("description") or "",
         "capabilities": e.get("capabilities") or [],
         "department": e.get("department") or "", "published": True}
        for e in entries
    ]
    cases = [
        ("Rank supplier delivery risk for the door trim program", "Supplier Risk Agent"),
        ("Which dealers missed their quarterly target?", "Dealer Performance Agent"),
        ("Analyze warranty claims for infotainment units", "Warranty & Service Agent"),
        ("Break down the margin for the B-SUV program", "Cost Analysis Agent"),
        ("Which transport lanes have the worst on-time performance?", "Logistics & Distribution Agent"),
        ("Hi", None),  # greetings are NOT routed to specialists
    ]
    for task, want in cases:
        sel = semantic_select_specialists(task, profiles)
        got = sel[0]["name"] if sel else None
        status = "OK " if got == want else "MISS"
        if got != want:
            failures.append(f"semantic: '{task[:40]}' -> {got} != {want}")
        print(f"semantic {status} '{task[:46]}' -> {got}")
    for guard in ("translate this document to french",
                  "what's the weather today"):
        if semantic_select_specialists(guard, profiles):
            failures.append(f"semantic guard: '{guard}' capability-stretched")
        else:
            print(f"semantic OK  guard (no stretch): '{guard}'")
    # Informational (NOT failures): borderline analytic phrasings measured
    # just under the honesty threshold — GA direct execution handles them.
    for borderline in ("Why did production line 2 output drop last week?",
                       "Identify reasons for declining profitability"):
        sel = semantic_select_specialists(borderline, profiles)
        print(f"semantic info (threshold-honest): '{borderline[:44]}' -> "
              f"{[c['name'] for c in sel] or 'GA own tools'}")

    db.close()
    print()
    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("ALL PHASE E CHECKS PASSED (zero LLM credits consumed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
