"""Phase 2 — enterprise scenarios A–H (deterministic; mocks only).

Each scenario exercises the REAL seam chain: classifier → decision model →
healing policy → (SSE payload shape where relevant). Mocked evidence is
integration-level, NOT live-verified.

Run: pytest tests/test_phase2_scenarios.py -q
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.services.execution_recovery import (
    FailureCategory,
    HealingBudget,
    classify_failure,
    should_reformulate,
    should_retry,
)
from app.services.execution_decisions import (
    ExecutionObservation,
    decision_from_review,
    evaluate_observation,
)
from app.services.evidence_validation import (
    compare_metric_values,
    validate_delegation_result,
    validate_query_result,
    validate_retrieval,
)
from app.services.capability_fallback import select_alternatives


def _obs(**kw):
    return ExecutionObservation(**kw)


# ── Scenario A — invalid tool arguments ──────────────────────────────────────
def test_scenario_a_invalid_arguments_detected_corrected_verified():
    result = {"success": False, "error": "Invalid arguments: 'query' is a required property"}
    cls = classify_failure(result)
    assert cls.category in (FailureCategory.invalid_arguments, FailureCategory.missing_required_parameter)
    assert should_retry(cls, attempts=0) is False          # never identical-args retry
    assert should_reformulate(cls) is True                 # correction path eligible
    d = evaluate_observation(_obs(
        is_success=False, category=cls.category.value,
        requires_modified_arguments=True, attempts=0,
        triggering_observation=result["error"],
    ))
    assert d.decision == "correct_arguments"
    assert "schema" in d.proposed_next_action.lower()
    # After correction the returned figures get verified before answering.
    v = validate_query_result({
        "success": True, "columns": ["model", "total_sales"],
        "rows": [["A", 100]], "source_id": "ds-1",
    }, required_columns=("model", "total_sales"))
    assert v.status == "validated"


# ── Scenario B — database timeout ────────────────────────────────────────────
def test_scenario_b_timeout_bounded_retry_then_truthful_limitation():
    cls = classify_failure({"success": False, "error": "query timed out after 30s"})
    assert cls.category == FailureCategory.timeout
    policy_attempts = 0
    assert should_retry(cls, attempts=policy_attempts, max_attempts=2) is True
    assert should_retry(cls, attempts=2, max_attempts=2) is False
    d = evaluate_observation(_obs(
        is_success=False, category="timeout", attempts=2, budget_left=True,
        triggering_observation="timed out",
    ))
    assert d.decision == "replan"  # objective preserved, plan regenerated
    # If replanning is also exhausted → honest failure, never a fake result.
    d2 = evaluate_observation(_obs(
        is_success=False, category="timeout", attempts=2, budget_left=False,
        triggering_observation="timed out",
    ))
    assert d2.decision in ("replan", "fail")


# ── Scenario C — missing data (profitability without costs) ─────────────────
def test_scenario_c_missing_cost_evidence_never_invented():
    revenue = {"success": True, "columns": ["model", "revenue"], "rows": [["A", 1000]],
               "source_id": "ds-sales"}
    cost = {"success": True, "columns": ["model"], "rows": [["A"]], "source_id": "ds-finance"}
    rv = validate_query_result(revenue, required_columns=("model", "revenue"))
    cv = validate_query_result(cost, required_columns=("model", "production_cost"))
    assert rv.status == "validated"
    assert cv.status == "invalid"  # cost data structurally absent
    # Reviewer names the gap → decision delegates evidence gathering.
    review = {
        "verdict": "needs_revision", "is_complete": False, "needs_evidence": True,
        "missing_evidence": ["production cost per vehicle model"],
        "unsupported_claims": ["profit margin 34% has no cost basis"],
    }
    d = decision_from_review(review, specialist_available=True,
                             objective="Calculate profitability by vehicle model")
    assert d.decision == "delegate_specialist"
    assert d.affected_task and "cost" in d.affected_task
    # Without any authorized source → finish with explicit limitations.
    d2 = decision_from_review(review, specialist_available=False)
    assert d2.decision == "replan"
    assert "never invent" in d2.proposed_next_action.lower() or "limitations" in d2.proposed_next_action.lower()


# ── Scenario D — multi-agent partial failure ────────────────────────────────
def test_scenario_d_partial_multi_agent_preserves_successes():
    results = {
        "success": True,
        "status": "partial",
        "actual_execution_strategy": "multi_agent",
        "summary": "1 completed, 0 partial, 1 failed",
        "results": [
            {"task": "sales trend", "agent_name": "sales", "success": True,
             "status": "completed", "response": "declining trend verified", "evidence_ids": ["e1"]},
            {"task": "stock levels", "agent_name": "inventory", "success": False,
             "status": "failed", "error": "connection refused"},
        ],
    }
    v = validate_delegation_result(results)
    assert v.status == "partial"  # honest partial — some real findings exist
    # Failed child classified as transient → bounded recovery; successes kept.
    cls = classify_failure(results["results"][1])
    assert cls.category == FailureCategory.connection_error
    d = decision_from_review({
        "verdict": "needs_revision", "is_complete": False, "needs_evidence": True,
        "missing_evidence": ["inventory availability"],
    }, specialist_available=True, objective="Investigate declining vehicle sales")
    assert d.decision == "delegate_specialist"


# ── Scenario E — contradictory evidence ──────────────────────────────────────
def test_scenario_e_contradiction_inspected_not_silently_resolved():
    verdict = compare_metric_values(120.0, 90.0)
    assert verdict == "contradiction"
    review = {
        "verdict": "needs_revision",
        "dimensions": {
            "contradictory_findings": {
                "status": "fail",
                "detail": "CRM reports 90 units, BI warehouse reports 120 units",
            },
        },
        "answered": True,
    }
    report_dec = decision_from_review(review, specialist_available=True)
    assert report_dec.decision == "request_peer_collaboration"
    # freshness/provenance inspection is part of the proposed action
    assert "authority" in report_dec.proposed_next_action.lower() or "provenance" in report_dec.proposed_next_action.lower()


# ── Scenario F — specialist unavailable ──────────────────────────────────────
def test_scenario_f_unpublished_specialist_never_used():
    candidates = [
        {"name": "demand_forecaster", "description": "Demand forecasting", "published": False},
        {"name": "sales_analyst", "description": "Sales revenue analysis", "published": True},
    ]
    out = select_alternatives(candidates, required_keywords=("demand", "forecast"))
    assert out == []  # the ONLY capability match is unpublished → refuse
    d = evaluate_observation(_obs(
        is_success=False, terminal=True, category="unsupported_operation",
        triggering_observation="required specialist unpublished",
        has_alternative=False,
    ))
    assert d.decision == "fail"
    assert "limitation" in d.proposed_next_action.lower()


# ── Scenario G — consequential action ────────────────────────────────────────
def test_scenario_g_approval_pause_never_auto_retried():
    approval = classify_failure({"success": False, "requires_approval": True})
    assert approval.terminal is True
    assert should_retry(approval, attempts=0, max_attempts=3) is False
    assert should_reformulate(approval) is False
    d = evaluate_observation(_obs(
        is_success=False, approval_pending=True,
        triggering_observation="submit purchase order requires confirmation",
    ))
    assert d.decision == "request_approval"
    assert d.requires_authorization is True
    assert "rerout" in d.proposed_next_action.lower() or "not proceed" in d.proposed_next_action.lower()


# ── Scenario H — long-running interruption ───────────────────────────────────
def test_scenario_h_orphan_recovery_exists_and_never_marks_completed():
    import inspect
    import re

    from app.services import agent_invocations as inv

    src = inspect.getsource(inv.recover_orphaned_invocations)
    # Worker crash must NEVER surface as a successful completion: no
    # executable status assignment to "completed".
    assert not re.search(r"status\s*=\s*[\"']completed[\"']", src)
    assert re.search(r"[\"']failed[\"']", src)
    # And it is scheduled (no second scheduler — reused infra).
    from app.services import scheduled_tasks as st
    st_src = inspect.getsource(st)
    assert "recover_orphaned_invocations" in st_src


def test_scenario_h_healing_budget_survives_tool_hops_within_turn():
    # One turn, many tools: total healing bounded across ALL of them.
    budget = HealingBudget(max_healings=4)
    consumed = sum(budget.consume(f"tool_{i}") for i in range(10) if budget.remaining > 0)
    assert consumed == 4 and budget.remaining == 0
