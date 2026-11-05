"""Phase 3.5 P6 — credit-free manufacturing acceptance suite (deterministic).

Runs the 10 Phase-3 manufacturing scenarios through the REAL deterministic
pipeline (planner → capability selection → failure classification → recovery
decision → evidence validation → final decision) with a mocked embedder and
mocked failure injections. ZERO LLM credits. Clearly NOT a model evaluation —
live runs stay gated on verified credentials (see provider diagnostics).

Also proves golden-format compatibility: scenarios are representable as
AgentTestCase records (input_json/expected_output_json) so the existing
golden_eval_runner can execute them live later — no second framework.

Run: pytest tests/test_manufacturing_acceptance.py -q
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services.semantic_capability_match import semantic_select_specialists
from app.services.durable_execution import ResumeAction, resume_decision
from app.services.evidence_validation import (
    compare_metric_values,
    validate_delegation_result,
    validate_query_result,
)
from app.services.execution_decisions import (
    ExecutionObservation,
    decision_from_review,
    evaluate_observation,
)
from app.services.execution_recovery import classify_failure
from app.services.turn_planner import build_turn_plan

_AGENTS = [
    {"name": "sales_analyst", "description": "Sales revenue, regional performance",
     "published": True, "department": "sales"},
    {"name": "production_agent", "description": "Production output, line efficiency, downtime",
     "published": True, "department": "manufacturing"},
    {"name": "quality_agent", "description": "Defect rates, quality inspections",
     "published": True, "department": "quality"},
    {"name": "procurement_agent", "description": "Supplier performance, purchasing",
     "published": True, "department": "procurement"},
    {"name": "inventory_agent", "description": "Inventory levels, shortages, stock",
     "published": True, "department": "supply_chain"},
    {"name": "finance_agent", "description": "Costs, margins, profitability",
     "published": True, "department": "finance"},
    {"name": "demand_forecaster", "description": "Demand forecasting, seasonality",
     "published": True, "department": "sales"},
]

_TOPICS = {"sales": 0, "production": 1, "quality": 2, "defect": 2, "cost": 3,
           "profitability": 3, "demand": 4, "forecast": 4, "supplier": 5,
           "inventory": 6, "stock": 6}


def _embedder(texts):
    out = []
    for t in texts:
        v = [0.0] * 7
        low = (t or "").lower()
        for topic, idx in _TOPICS.items():
            if topic in low:
                v[idx] = 1.0
        out.append(v)
    return out


def _selection_for(task):
    return semantic_select_specialists(task, _AGENTS, embedder=_embedder, top_k=2)


# (scenario, objective, top_specialist|None, injected_failure, expected_decision)
SCENARIOS = [
    ("sales_performance", "Analyze sales performance by region this quarter",
     "sales_analyst", None, "complete"),
    ("production_efficiency", "Analyze monthly production efficiency trends",
     "production_agent", {"error": "查询超时"}, "retry"),
    ("quality_defects", "Which product lines have the highest defect rates?",
     "quality_agent", None, "complete"),
    ("inventory_shortage", "Investigate inventory shortages for top SKUs",
     "inventory_agent", {"error": "connection refused"}, "retry"),
    ("supplier_performance", "Evaluate supplier delivery performance",
     "procurement_agent", None, "complete"),
    ("procurement_analysis", "Review purchasing spend by supplier",
     "procurement_agent", None, "complete"),
    ("financial_profitability", "Calculate profitability by vehicle model",
     "finance_agent", {"review_missing": "production cost per model"},
     "delegate_specialist"),
    ("cross_department_ops", "Investigate declining production output across departments",
     "production_agent", None, "complete"),
    ("forecast_deviation", "Analyze demand forecast deviation last quarter",
     "demand_forecaster", None, "complete"),
    ("executive_report", "Prepare the monthly enterprise performance report",
     None, None, "complete"),
]


@pytest.mark.parametrize(
    "name,objective,top,failure,expected_decision",
    SCENARIOS, ids=[s[0] for s in SCENARIOS],
)
def test_acceptance_scenario_pipeline(name, objective, top, failure, expected_decision):
    # 1. Intent understanding + plan (deterministic template path).
    plan = build_turn_plan(objective)
    assert plan.kind != "none"
    assert plan.intent_type in ("unknown", "conversational")
    assert plan.execution_strategy in ("direct_tool", "single_specialist",
                                       "multi_agent", "plan_dag")

    # 2. Specialist selection (mocked deterministic embeddings).
    sel = _selection_for(objective)
    if top is None:
        # GA prefers its own tools for general/report objectives.
        assert top is None and (sel == [] or sel[0]["name"] != "quality_agent")
    else:
        assert sel and sel[0]["name"] == top, (
            f"{name}: {[c['name'] for c in sel]} != {top}"
        )

    # 3-6. Failure → classification → decision (or clean completion).
    if failure is None:
        d = evaluate_observation(ExecutionObservation(
            is_success=True, triggering_observation="tool succeeded",
            objective=objective,
        ))
    elif "review_missing" in failure:
        d = decision_from_review(
            {"verdict": "needs_revision", "needs_evidence": True,
             "missing_evidence": [failure["review_missing"]]},
            specialist_available=True, objective=objective,
        )
    else:
        cls = classify_failure({"success": False, "error": failure["error"]})
        d = evaluate_observation(ExecutionObservation(
            is_success=False, category=cls.category.value, attempts=0,
            budget_left=True, triggering_observation=failure["error"],
            objective=objective,
        ))
    assert d.decision == expected_decision, (
        f"{name}: {d.decision} != {expected_decision}"
    )

    # 7. Honest final status: every decision is representable without
    # fabrication (no 'complete' on failures; limitations named otherwise).
    if expected_decision != "complete":
        assert d.proposed_next_action


def test_approval_and_destructive_scenarios_stay_gated():
    # "Submit the purchase order" class of scenarios: consequential writes
    # are NEVER auto-resumed and NEVER executed by recovery.
    d = resume_decision(
        status="running",
        started_at=datetime.now(timezone.utc) - timedelta(hours=1),
        mutating=True, requires_confirmation=True,
        plan_id="p", seq=1, name="submit po",
    )
    assert d.action in (ResumeAction.manual, ResumeAction.skip)
    assert d.requires_authorization is True


def test_contradiction_scenario_explains_never_picks():
    # Scenario E (Phase 2) holds in the acceptance suite: two sources
    # disagreeing is a contradiction to EXPLAIN, not a number to pick.
    assert compare_metric_values(120.0, 90.0) == "contradiction"
    d = decision_from_review(
        {"verdict": "needs_revision",
         "dimensions": {"contradictory_findings": {
             "status": "fail",
             "detail": "CRM reports 90 units, BI warehouse reports 120"}}},
        specialist_available=True,
    )
    assert d.decision == "request_peer_collaboration"


def test_partial_multi_agent_result_is_truthful():
    r = validate_delegation_result({
        "success": True, "status": "partial", "truncated": True,
        "response": "half the investigation", "evidence_ids": ["e1"],
    })
    assert r.status == "partial"


def test_golden_format_compatibility_no_second_framework():
    """Scenarios are representable as AgentTestCase records so the EXISTING
    golden_eval_runner can run them live later."""
    from types import SimpleNamespace

    from app.services.golden_eval_runner import _parse_case_payload

    case = SimpleNamespace(
        agent_app_id="app-general",
        name="manufacturing:production_efficiency",
        test_type="golden",
        status="active",
        input_json=json.dumps({
            "message": "Analyze monthly production efficiency trends",
            "injected_failure": {"error": "查询超时"},
        }),
        expected_output_json=json.dumps({
            "failure_category": "timeout",
            "expected_decision": "retry",
            "honesty": "bounded retry or truthful limitation",
        }),
    )
    payload = _parse_case_payload(case)
    assert payload["input"]["injected_failure"]["error"] == "查询超时"
    assert payload["expected"]["failure_category"] == "timeout"
    # And the recorded expectation matches the real classifier:
    cls = classify_failure({"success": False, "error": "查询超时"})
    assert cls.category.value == payload["expected"]["failure_category"]
