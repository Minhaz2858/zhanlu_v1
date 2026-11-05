"""Phase 1 — GA intent understanding, plan enrichment, strategy, reviewer-driven
replanning (P1.1–P1.4 + scenario evaluation P1.6).

Deterministic tests — the LLM planner is exercised via canned planner outputs
(parse_dynamic_plan is a pure function), so no provider is needed. Live LLM
verification remains a separate acceptance gate.

Run: pytest tests/test_phase1_intent_planning.py -q
"""
import json

from app.services.turn_planner import (
    EXECUTION_STRATEGIES,
    INTENT_TYPES,
    RISK_LEVELS,
    build_turn_plan,
    dynamic_plan_prompt,
    parse_dynamic_plan,
    plan_meta_frame,
)
from app.services.synexia.replan_policy import evaluate_reviewer_findings


# ── P1.1: intent taxonomy ────────────────────────────────────────────────────
def test_intent_taxonomy_complete():
    # Phase 1.5: "unknown" is a first-class intent; "plan_dag" is a strategy.
    assert set(INTENT_TYPES) == {
        "conversational", "information_retrieval", "data_analysis", "research",
        "investigation", "decision_support", "report_generation",
        "operational_execution", "multi_step_task", "cross_department_task",
        "unknown",
    }
    assert set(RISK_LEVELS) == {"low", "medium", "high", "critical"}
    assert set(EXECUTION_STRATEGIES) == {
        "direct_response", "direct_tool", "single_specialist", "multi_agent",
        "plan_dag",
    }


# ── P1.1/P1.2: LLM plan path carries enriched, validated meta ────────────────
def test_dynamic_plan_parses_enriched_meta():
    raw = json.dumps({
        "intent_type": "investigation",
        "objective_statement": "Explain last month's vehicle sales decline",
        "expected_outcome": "Verified contributing factors",
        "risk_level": "medium",
        "required_approvals": False,
        "execution_strategy": "multi_agent",
        "constraints": ["read-only data access"],
        "acceptance_criteria": ["each cause backed by evidence", "no invented figures"],
        "verification_requirements": ["reviewer verdict approved"],
        "steps": [
            {"title": "Retrieve sales performance data"},
            {"title": "Analyze dealer and inventory data"},
            {"title": "Verify findings against evidence"},
        ],
    })
    plan = parse_dynamic_plan(raw, "generic")
    assert plan is not None
    assert plan.intent_type == "investigation"
    assert plan.objective_statement == "Explain last month's vehicle sales decline"
    assert plan.expected_outcome == "Verified contributing factors"
    assert plan.risk_level == "medium"
    assert plan.required_approvals is False
    assert plan.execution_strategy == "multi_agent"
    assert plan.constraints == ("read-only data access",)
    assert plan.acceptance_criteria == ("each cause backed by evidence", "no invented figures")
    assert plan.verification_requirements == ("reviewer verdict approved",)
    assert len(plan.steps) == 3


def test_invalid_meta_values_dropped_but_plan_survives():
    raw = json.dumps({
        "intent_type": "make_money_fast",       # not in taxonomy
        "risk_level": "extreme",                # not a risk level
        "execution_strategy": "yolo",           # not a strategy
        "required_approvals": "yes",            # truthy string → True
        "acceptance_criteria": "single string", # scalar → wrapped
        "steps": [{"title": "A"}, {"title": "B"}],
    })
    plan = parse_dynamic_plan(raw, "generic")
    assert plan is not None and len(plan.steps) == 2
    # Phase 1.5: unclassified intent is represented explicitly as "unknown".
    assert plan.intent_type == "unknown"
    assert plan.risk_level is None
    # Invalid strategy degrades to the conservative default (never guessed).
    assert plan.execution_strategy == "direct_tool"
    assert plan.required_approvals is True
    assert plan.acceptance_criteria == ("single string",)


def test_steps_only_plan_backward_compatible():
    plan = parse_dynamic_plan(json.dumps({"steps": [{"title": "A"}, {"title": "B"}]}), "data")
    assert plan is not None and len(plan.steps) == 2
    assert plan.intent_type == "unknown"
    assert plan.acceptance_criteria == ()


def test_numbered_list_fallback_still_parses_without_meta():
    plan = parse_dynamic_plan("1. Gather data\n2. Analyze\n3. Verify", "data")
    assert plan is not None and len(plan.steps) == 3
    assert plan.intent_type == "unknown"


# ── P1.3: template path — strategy default, explicit meta ────────────────────
def test_template_plan_default_strategy_by_kind():
    none_plan = build_turn_plan("Good morning.")
    assert none_plan.kind == "none"
    assert none_plan.execution_strategy == "direct_response"

    data_plan = build_turn_plan("What was our total vehicle sales revenue last month?",
                                tool_names=["nl2sql"])
    assert data_plan.kind == "data"
    assert data_plan.execution_strategy == "direct_tool"


def test_template_plan_accepts_explicit_meta():
    plan = build_turn_plan(
        "Investigate the sales decline",
        plan_meta={"intent_type": "investigation",
                   "objective_statement": "Explain the decline",
                   "risk_level": "medium",
                   "acceptance_criteria": ["evidence-backed causes"]},
    )
    assert plan.intent_type == "investigation"
    assert plan.objective_statement == "Explain the decline"
    assert plan.risk_level == "medium"
    assert plan.acceptance_criteria == ("evidence-backed causes",)
    # strategy default still applied when not provided
    assert plan.execution_strategy == "direct_tool"
    # invalid values dropped, not crashed (intent degrades to explicit "unknown")
    bad = build_turn_plan("x", plan_meta={"intent_type": "bogus", "risk_level": 42})
    assert bad.intent_type == "unknown" and bad.risk_level is None


def test_plan_meta_omits_empty_fields():
    # A greeting carries its truthful classification (Phase 1.5: intent is
    # always represented) — but nothing else is fabricated.
    assert build_turn_plan("Good morning.").plan_meta() == {
        "intent_type": "conversational",
        "execution_strategy": "direct_response",
    }
    meta = build_turn_plan(
        "Prepare a report", is_report_request=True,
        plan_meta={"intent_type": "report_generation", "risk_level": "low"},
    ).plan_meta()
    assert meta["intent_type"] == "report_generation"
    assert meta["risk_level"] == "low"
    assert "objective_statement" not in meta
    assert "acceptance_criteria" not in meta


# ── P1.5: plan-meta SSE frame (content-invariant, existing event style) ─────
def test_plan_meta_frame_none_when_empty():
    from app.services.turn_planner import TurnPlan

    # A bare plan with no meta at all emits no frame (no fake events).
    assert plan_meta_frame(TurnPlan()) is None


def test_plan_meta_frame_scrubs_identifiers():
    plan = build_turn_plan("x", plan_meta={"objective_statement": "Summarize v_sale_order"})
    frame = plan_meta_frame(plan)
    assert frame is not None and frame.startswith("data: ")
    payload = json.loads(frame[len("data: "):].strip())
    assert payload["type"] == "plan_meta"
    assert payload["objective_statement"] == "[data]"


def test_planner_prompt_documents_meta_fields():
    system, _msgs = dynamic_plan_prompt("Investigate sales decline", "generic")
    assert "intent_type" in system
    assert "acceptance_criteria" in system
    assert "risk_level" in system


# ── P1.4: reviewer-driven structured findings → replan decision ─────────────
def test_no_review_means_no_replan():
    d = evaluate_reviewer_findings(None)
    assert d["should_replan"] is False
    assert d["reason"] == "no_review"
    d = evaluate_reviewer_findings({})
    assert d["should_replan"] is False


def test_approved_complete_review_needs_no_replan():
    d = evaluate_reviewer_findings({"verdict": "approved", "is_complete": True})
    assert d["should_replan"] is False


def test_missing_evidence_triggers_gather_action():
    d = evaluate_reviewer_findings({
        "verdict": "needs_revision", "is_complete": False,
        "needs_evidence": True, "missing_evidence": ["verified cost data per model"],
    })
    assert d["should_replan"] is True
    assert d["needs_evidence"] is True
    assert d["missing_evidence"] == ["verified cost data per model"]
    assert d["recommended_next_action"] == "gather_missing_evidence"


def test_unsupported_claims_trigger_revise_action():
    d = evaluate_reviewer_findings({
        "verdict": "needs_revision", "is_complete": True,
        "unsupported_claims": ["profit margin 34% has no cost basis"],
    })
    assert d["should_replan"] is True
    assert d["recommended_next_action"] == "revise_response"


def test_contradictions_trigger_resolve_action():
    d = evaluate_reviewer_findings({
        "verdict": "needs_revision",
        "contradictions": ["revenue up in table A, down in table B"],
    })
    assert d["should_replan"] is True
    assert d["recommended_next_action"] == "resolve_contradictions"


def test_failed_acceptance_criteria_trigger_action():
    d = evaluate_reviewer_findings({
        "verdict": "approved", "is_complete": True,
        "failed_acceptance_criteria": ["per-model breakdown missing"],
    })
    assert d["should_replan"] is True
    assert d["recommended_next_action"] == "address_failed_criteria"


# ── P1.6: enterprise scenarios (deterministic planner-level) ────────────────
def test_scenario1_conversational_no_specialist_machinery():
    # "Can you explain what OEE means?" — template produces no delegation
    # machinery; an LLM planner classifying it stays conversational.
    plan = build_turn_plan("Can you explain what OEE means?")
    assert plan.kind in ("none", "generic")
    plan2 = parse_dynamic_plan(json.dumps({
        "intent_type": "conversational", "execution_strategy": "direct_response",
        "steps": [{"title": "Explain OEE"}, {"title": "Give a factory example"}],
    }), "generic")
    assert plan2.intent_type == "conversational"
    assert plan2.execution_strategy == "direct_response"


def test_scenario2_retrieval_plan():
    plan = parse_dynamic_plan(json.dumps({
        "intent_type": "information_retrieval", "execution_strategy": "direct_tool",
        "objective_statement": "Total vehicle sales revenue last month",
        "steps": [{"title": "Query sales revenue"}, {"title": "Report the figure"}],
    }), "data")
    assert plan.intent_type == "information_retrieval"
    assert plan.execution_strategy == "direct_tool"


def test_scenario4_multi_agent_investigation():
    plan = parse_dynamic_plan(json.dumps({
        "intent_type": "investigation", "execution_strategy": "multi_agent",
        "objective_statement": "Why did vehicle sales decline last month",
        "acceptance_criteria": ["causes distinguished from hypotheses"],
        "steps": [
            {"title": "Retrieve sales data"}, {"title": "Analyze dealer performance"},
            {"title": "Check inventory availability"}, {"title": "Verify causes"},
        ],
    }), "generic")
    assert plan.intent_type == "investigation"
    assert plan.execution_strategy == "multi_agent"
    assert len(plan.steps) == 4
    assert "causes distinguished from hypotheses" in plan.acceptance_criteria


def test_scenario7_missing_evidence_never_invented():
    # Planner encodes the cost-data acceptance criterion…
    plan = parse_dynamic_plan(json.dumps({
        "intent_type": "data_analysis", "execution_strategy": "direct_tool",
        "acceptance_criteria": ["per-model profit uses verified cost data"],
        "verification_requirements": ["no profit figure without cost evidence"],
        "steps": [{"title": "Check cost data availability"}, {"title": "Compute or report limitation"}],
    }), "data")
    assert "per-model profit uses verified cost data" in plan.acceptance_criteria
    # …and the reviewer's structured findings drive the honest outcome.
    d = evaluate_reviewer_findings({
        "verdict": "needs_revision", "is_complete": False, "needs_evidence": True,
        "missing_evidence": ["cost data for every vehicle model"],
    })
    assert d["should_replan"] is True
    assert d["missing_evidence"] == ["cost data for every vehicle model"]
    assert d["recommended_next_action"] == "gather_missing_evidence"
