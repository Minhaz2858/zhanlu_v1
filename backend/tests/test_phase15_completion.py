"""Phase 1.5 — GA intelligence completion: intent consistency, reviewer-guidance
execution, plan-meta UI, strategy/actual consistency.

Deterministic tests only — no live LLM. The reviewer path is exercised by
patching ``review_findings`` and ``_run_sub_agent`` (pure orchestration logic).

Run: pytest tests/test_phase15_completion.py -q
"""
import asyncio
import json

import pytest

from app.services.turn_planner import (
    EXECUTION_STRATEGIES,
    INTENT_TYPES,
    build_turn_plan,
    parse_dynamic_plan,
    plan_to_system_block,
)


# ── Gap 1/4: taxonomy completeness ───────────────────────────────────────────
def test_taxonomy_includes_unknown_and_plan_dag():
    # Gap 1: "unknown" is a FIRST-CLASS intent value — absence of classification
    # is represented explicitly, never omitted.
    assert "unknown" in INTENT_TYPES
    # Gap 4: DAG dispatch is an executable strategy (plan_dag).
    assert "plan_dag" in EXECUTION_STRATEGIES


def test_plan_dag_strategy_accepted_by_validator():
    plan = parse_dynamic_plan(json.dumps({
        "execution_strategy": "plan_dag",
        "steps": [{"title": "A"}, {"title": "B"}],
    }), "generic")
    assert plan.execution_strategy == "plan_dag"


# ── Gap 1: intent consistency — template path ────────────────────────────────
def test_template_greeting_intent_is_conversational():
    plan = build_turn_plan("Good morning.")
    assert plan.kind == "none"
    assert plan.intent_type == "conversational"


def test_template_task_intent_is_unknown():
    # The template path cannot classify semantically — it must say so
    # explicitly instead of omitting intent_type.
    for kwargs in (
        {},
        {"tool_names": ["nl2sql"]},          # data plan
        {"is_report_request": True},         # report plan
        {"is_dashboard_build": True},        # dashboard plan
    ):
        plan = build_turn_plan("Investigate the sales decline", **kwargs)
        assert plan.kind != "none"
        assert plan.intent_type == "unknown", f"kind={plan.kind}"


def test_llm_plan_missing_intent_defaults_to_unknown():
    plan = parse_dynamic_plan(json.dumps({"steps": [{"title": "A"}, {"title": "B"}]}), "data")
    assert plan.intent_type == "unknown"


def test_plan_meta_always_carries_intent():
    # Both paths: intent is explicitly represented in plan_meta (never absent).
    assert "intent_type" in build_turn_plan("Good morning.").plan_meta()
    assert "intent_type" in build_turn_plan("Write a poem").plan_meta()
    llm = parse_dynamic_plan(json.dumps({"steps": [{"title": "A"}, {"title": "B"}]}), "data")
    assert "intent_type" in llm.plan_meta()


def test_explicit_llm_intent_still_wins_over_defaults():
    plan = parse_dynamic_plan(json.dumps({
        "intent_type": "investigation",
        "steps": [{"title": "A"}, {"title": "B"}],
    }), "generic")
    assert plan.intent_type == "investigation"


# ── Gap 2/4: the loop SEES the plan meta (acceptance criteria, risk, …) ──────
def _enriched_plan():
    return build_turn_plan(
        "Investigate the sales decline",
        plan_meta={
            "intent_type": "investigation",
            "objective_statement": "Explain last month's decline",
            "expected_outcome": "Verified contributing factors",
            "constraints": ["read-only data access"],
            "acceptance_criteria": ["each cause backed by evidence"],
            "verification_requirements": ["reviewer verdict approved"],
            "risk_level": "medium",
            "execution_strategy": "multi_agent",
        },
    )


def test_plan_to_system_block_includes_meta():
    block = plan_to_system_block(_enriched_plan())
    assert "Explain last month's decline" in block
    assert "each cause backed by evidence" in block
    assert "reviewer verdict approved" in block
    assert "medium" in block
    assert "multi_agent" in block
    assert "read-only data access" in block


def test_plan_to_system_block_plain_when_no_meta():
    block = plan_to_system_block(build_turn_plan("Write a poem"))
    assert "TURN PLAN" in block
    assert "Acceptance criteria" not in block


# ── Gap 2: reviewer guidance drives a BOUNDED targeted continuation ──────────
class _FakeReport:
    def __init__(self, d, needs_evidence):
        self._d = d
        self.needs_evidence = needs_evidence

    def to_dict(self):
        return self._d


def _patch_review(monkeypatch, reports):
    calls = {"review": 0}

    def fake_review(*args, **kwargs):
        d = reports[min(calls["review"], len(reports) - 1)]
        calls["review"] += 1
        return d

    monkeypatch.setattr(
        "app.services.agent_collaboration.reviewer.review_findings", fake_review
    )
    return calls


def _patch_sub_agent(monkeypatch, calls_log):
    async def fake_run(task, agent_name, db, user_id, max_iterations, endpoint, context=None):
        calls_log.append({"task": task, "context": dict(context or {})})
        return {"success": True, "response": "gap finding", "evidence_ids": ["e1"]}

    monkeypatch.setattr(
        "app.services.tool_handlers.delegate_tool._run_sub_agent", fake_run
    )


def test_reviewer_gap_runs_bounded_followup(monkeypatch):
    from app.services.tool_handlers import delegate_tool as dt

    reports = [
        _FakeReport(
            {"verdict": "needs_revision", "is_complete": False, "needs_evidence": True,
             "missing_evidence": ["cost data per model"]},
            needs_evidence=["cost data per model"],
        ),
        _FakeReport({"verdict": "approved", "is_complete": True}, needs_evidence=[]),
    ]
    calls = _patch_review(monkeypatch, reports)
    subs = []
    _patch_sub_agent(monkeypatch, subs)

    result = asyncio.run(dt._maybe_review_and_refine(
        "Investigate the decline", "analyst", None, None, None,
        {"_enable_review": True},
        {"success": True, "response": "draft"},
    ))

    # exactly ONE follow-up round; the follow-up context does NOT re-enable
    # review (no review loops, no duplicate delegation)
    assert len(subs) == 1
    assert "_enable_review" not in subs[0]["context"]
    assert "cost data per model" in subs[0]["task"]
    assert "gap finding" in result["response"]
    # re-review ran exactly once more and its verdict won
    assert calls["review"] == 2
    assert result["review"]["verdict"] == "approved"
    # structured guidance attached for the parent LLM
    assert result["review_guidance"]["should_replan"] is False
    assert result["evidence_ids"] == ["e1"]


def test_reviewer_approval_runs_no_followup(monkeypatch):
    from app.services.tool_handlers import delegate_tool as dt

    reports = [_FakeReport({"verdict": "approved", "is_complete": True}, needs_evidence=[])]
    _patch_review(monkeypatch, reports)
    subs = []
    _patch_sub_agent(monkeypatch, subs)

    result = asyncio.run(dt._maybe_review_and_refine(
        "t", "a", None, None, None, {"_enable_review": True},
        {"success": True, "response": "ok"},
    ))
    assert subs == []
    assert result["review_guidance"]["should_replan"] is False


def test_reviewer_capped_at_two_gaps(monkeypatch):
    from app.services.tool_handlers import delegate_tool as dt

    reports = [
        _FakeReport(
            {"verdict": "needs_revision", "needs_evidence": True,
             "missing_evidence": ["g1", "g2", "g3"]},
            needs_evidence=["g1", "g2", "g3"],
        ),
        _FakeReport({"verdict": "approved", "is_complete": True}, needs_evidence=[]),
    ]
    _patch_review(monkeypatch, reports)
    subs = []
    _patch_sub_agent(monkeypatch, subs)

    result = asyncio.run(dt._maybe_review_and_refine(
        "t", "a", None, None, None, {"_enable_review": True},
        {"success": True, "response": "ok"},
    ))
    assert len(subs) == 2  # 3 gaps requested, hard cap 2
    assert result["review"]["verdict"] == "approved"


# ── Gap 4: recorded strategy vs ACTUAL execution consistency ────────────────
def test_single_delegation_labels_single_specialist(monkeypatch):
    from app.services.tool_handlers import delegate_tool as dt

    async def fake_run(*a, **k):
        return {"success": True, "response": "done"}

    monkeypatch.setattr(dt, "_run_sub_agent", fake_run)
    result = asyncio.run(dt._delegate_task({"task": "check stock"}, None, None, context={}))
    assert result["actual_execution_strategy"] == "single_specialist"


def test_batch_delegation_labels_multi_agent(monkeypatch):
    from app.services.tool_handlers import delegate_tool as dt

    async def fake_run(*a, **k):
        return {"success": True, "response": "done"}

    monkeypatch.setattr(dt, "_run_sub_agent", fake_run)
    result = asyncio.run(dt._delegate_batch(
        [{"task": "a", "agent_name": "x", "max_iterations": 3},
         {"task": "b", "agent_name": "y", "max_iterations": 3}],
        None, None, None, {},
    ))
    assert result["actual_execution_strategy"] == "multi_agent"


# ── Gap 3 is frontend — see LiveActivityStream.test.jsx ──────────────────────
def test_sanity_meta_roundtrip():
    raw = json.dumps({
        "intent_type": "unknown",
        "execution_strategy": "plan_dag",
        "acceptance_criteria": "single string",
        "steps": [{"title": "A"}, {"title": "B"}],
    })
    plan = parse_dynamic_plan(raw, "generic")
    meta = plan.plan_meta()
    assert meta["intent_type"] == "unknown"
    assert meta["execution_strategy"] == "plan_dag"
    assert meta["acceptance_criteria"] == ["single string"]


@pytest.mark.parametrize("bad", [None, 42, "x"])
def test_norm_meta_tolerates_garbage(bad):
    from app.services.turn_planner import _norm_plan_meta

    assert _norm_plan_meta(bad) == {}
