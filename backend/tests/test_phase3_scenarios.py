"""Phase 3 — cross-department acceptance scenarios (G) + security regression (H).

Deterministic chains through the REAL seams: planning → capability discovery
(semantic+lexical) → execution classification → recovery decisions →
evidence validation → reviewer-driven correction → truthful synthesis
constraints. Mocked = integration-level, NOT live-verified.

Run: pytest tests/test_phase3_scenarios.py -q
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services.semantic_capability_match import semantic_select_specialists
from app.services.capability_fallback import select_alternatives
from app.services.durable_execution import (
    ResumeAction,
    STALE_RUNNING_TTL_S,
    resume_decision,
)
from app.services.evidence_validation import (
    compare_metric_values,
    validate_delegation_result,
    validate_query_result,
)
from app.services.execution_decisions import decision_from_review
from app.services.execution_recovery import (
    FailureCategory,
    classify_failure,
)


AGENT_DIR = [
    {"name": "production_agent", "description": "Production output, line efficiency, downtime",
     "published": True, "department": "manufacturing"},
    {"name": "inventory_agent", "description": "Inventory levels, shortages, material availability",
     "published": True, "department": "supply_chain"},
    {"name": "maintenance_agent", "description": "Machine downtime and maintenance records",
     "published": True, "department": "manufacturing"},
    {"name": "finance_agent", "description": "Costs, margins, profitability",
     "published": True, "department": "finance"},
    {"name": "sales_analyst", "description": "Sales revenue, regional trends",
     "published": True, "department": "sales"},
    {"name": "procurement_agent", "description": "Supplier performance, purchasing",
     "published": True, "department": "procurement"},
    {"name": "quality_agent", "description": "Defect rates, quality metrics",
     "published": True, "department": "quality"},
]


# ── Scenario 1: production decline ───────────────────────────────────────────
def test_scenario_1_production_decline_chain():
    # Discovery: production + maintenance (not sales).
    out = semantic_select_specialists(
        "why did production output decline last month", AGENT_DIR,
        embedder="auto", top_k=3,
    )
    names = [c["name"] for c in out]
    assert "production_agent" in names or "maintenance_agent" in names
    assert "sales_analyst" not in names[:1]
    # Execution: inventory tool times out → bounded retry.
    cls = classify_failure({"success": False, "error": "查询库存超时"})
    assert cls.category == FailureCategory.timeout
    # Reviewer finds missing downtime evidence → delegate maintenance.
    d = decision_from_review(
        {"verdict": "needs_revision", "needs_evidence": True,
         "missing_evidence": ["machine downtime records for the decline window"]},
        specialist_available=True,
        objective="Why did production output decline last month?",
    )
    assert d.decision == "delegate_specialist"
    # Final synthesis gated on evidence validation.
    v = validate_query_result({
        "success": True, "columns": ["line", "output"], "rows": [["L1", 420]],
        "source_id": "ds-mfg",
    }, required_columns=("line", "output"))
    assert v.status == "validated"


# ── Scenario 2: profitability by vehicle model ──────────────────────────────
def test_scenario_2_profitability_never_invented():
    revenue = {"success": True, "columns": ["model", "revenue"], "rows": [["A", 5000]],
               "source_id": "ds-sales"}
    costs = {"success": True, "columns": ["model"], "rows": [["A"]],
             "source_id": "ds-finance"}
    assert validate_query_result(revenue, required_columns=("model", "revenue")).status == "validated"
    assert validate_query_result(costs, required_columns=("model", "production_cost")).status == "invalid"
    d = decision_from_review(
        {"verdict": "needs_revision", "needs_evidence": True,
         "missing_evidence": ["authoritative production cost per model"],
         "unsupported_claims": ["margin asserted without cost basis"]},
        specialist_available=True,
        objective="Show profitability by vehicle model",
    )
    assert d.decision == "delegate_specialist"
    assert "cost" in (d.affected_task or "")
    # If costs cannot be found anywhere → final answer with explicit
    # limitations, never invented figures.
    d2 = decision_from_review(
        {"verdict": "needs_revision", "needs_evidence": True,
         "missing_evidence": ["authoritative production cost per model"]},
        specialist_available=False,
    )
    assert d2.decision == "replan"
    assert "never invent" in d2.proposed_next_action.lower() or "limitations" in d2.proposed_next_action.lower()


# ── Scenario 3: supplier operational risk ────────────────────────────────────
def test_scenario_3_supplier_risk_selection():
    out = select_alternatives(
        AGENT_DIR, required_keywords=("supplier", "defect", "delivery"))
    # Procurement (supplier+delivery) first; quality (defect rates) is a
    # legitimate collaborator — both capability matches, ranked by overlap.
    assert [c["name"] for c in out] == ["procurement_agent", "quality_agent"]
    # Cross-source defect-rate disagreement → explain, never pick silently.
    assert compare_metric_values(2.1, 4.8) == "contradiction"


# ── Scenario 4: regional sales decline ──────────────────────────────────────
def test_scenario_4_sales_decline_partial_preserved():
    results = {
        "success": True, "status": "partial",
        "summary": "1 completed, 1 failed",
        "results": [
            {"task": "regional trend", "agent_name": "sales_analyst",
             "success": True, "status": "completed", "evidence_ids": ["e1"]},
            {"task": "demand change", "agent_name": "demand_forecaster",
             "success": False, "status": "failed", "error": "connection reset"},
        ],
    }
    assert validate_delegation_result(results).status == "partial"
    # Failed child is transient → recovery eligible; successes preserved.
    cls = classify_failure(results["results"][1])
    assert cls.retryable is True


# ── Scenario 5+6: executive report + interrupted worker ─────────────────────
def test_scenario_6_restart_during_executive_report():
    # Persisted plan state across a restart: completed sections stay
    # completed; stale running finance section (read-only) recovers; stale
    # running approval-gated publish step NEVER auto-resumes.
    plan_id, now = "plan-exec-1", datetime.now(timezone.utc)
    stale = now - timedelta(seconds=STALE_RUNNING_TTL_S + 60)

    nodes = [
        {"status": "completed", "started_at": stale, "mutating": False},
        {"status": "completed", "started_at": stale, "mutating": False},
        {"status": "running", "started_at": stale, "mutating": False},
        {"status": "running", "started_at": stale, "mutating": True,
         "requires_confirmation": True},
    ]
    acts = [
        resume_decision(status=n["status"], started_at=n["started_at"],
                        mutating=n["mutating"],
                        requires_confirmation=n.get("requires_confirmation", False),
                        plan_id=plan_id, seq=i, name=f"node-{i}")
        for i, n in enumerate(nodes)
    ]
    assert [a.action for a in acts] == [
        ResumeAction.skip, ResumeAction.skip,
        ResumeAction.recover, ResumeAction.manual,
    ]
    # Completed nodes are NEVER re-executed and keys are deterministic.
    assert acts[0].idempotency_key == resume_decision(
        status="completed", started_at=stale,
        plan_id=plan_id, seq=0, name="node-0",
    ).idempotency_key


# ── Security H — recovery/fallback never bypass authorization ────────────────
def test_security_recovery_never_bypasses_approval():
    d = resume_decision(status="running",
                        started_at=datetime.now(timezone.utc) - timedelta(hours=6),
                        mutating=True, requires_confirmation=True)
    assert d.action == ResumeAction.manual
    assert d.requires_authorization is True


def test_security_fallback_never_selects_unpublished():
    out = semantic_select_specialists(
        "production cost analysis",
        [{"name": "draft_finance", "description": "cost analysis", "published": False}],
        embedder="auto",
    )
    assert out == []


def test_security_semantic_match_honors_department_wall():
    out = semantic_select_specialists(
        "cost analysis",
        [{"name": "finance_agent", "description": "cost analysis",
          "published": True, "department": "finance"}],
        embedder="auto", department="manufacturing",
    )
    assert out == []
