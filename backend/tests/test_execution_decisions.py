"""Phase 2.2 — observation-driven execution decision model (TDD).

A PURE decision table: observation (tool/specialist outcome + evidence state)
→ structured ExecutionDecision from the 11-value taxonomy. No chain-of-thought
is carried — only observation reference, evidence refs, reason code, affected
task, proposed next action, budget impact, authorization requirement.

Run: pytest tests/test_execution_decisions.py -q
"""
import pytest

from app.services.execution_decisions import (
    DECISION_TYPES,
    ExecutionDecision,
    ExecutionObservation,
    evaluate_observation,
    decision_from_review,
)


# ── Taxonomy ─────────────────────────────────────────────────────────────────
def test_decision_taxonomy_complete():
    assert DECISION_TYPES == {
        "continue", "retry", "correct_arguments", "select_alternative_tool",
        "delegate_specialist", "request_peer_collaboration", "replan",
        "request_clarification", "request_approval", "complete", "fail",
    }


# ── Decision table ───────────────────────────────────────────────────────────
def test_approval_pending_requests_approval_with_authorization():
    obs = ExecutionObservation(
        is_success=False, approval_pending=True,
        triggering_observation="write tool requires confirmation",
    )
    d = evaluate_observation(obs)
    assert d.decision == "request_approval"
    assert d.requires_authorization is True
    assert d.decision in DECISION_TYPES


def test_permission_denied_fails_never_routes_around():
    obs = ExecutionObservation(
        is_success=False, terminal=True, category="permission_denied",
        triggering_observation="Permission denied: table finances",
        specialist_available=True, has_alternative=True,
    )
    d = evaluate_observation(obs)
    # Even with alternatives and specialists available, an authorization
    # boundary must never be routed around.
    assert d.decision == "fail"
    assert d.requires_authorization is True
    assert d.reason_code == "authorization_boundary"


def test_unsupported_operation_selects_alternative_when_available():
    obs = ExecutionObservation(
        is_success=False, terminal=True, category="unsupported_operation",
        triggering_observation="Only SELECT statements are allowed",
        has_alternative=True,
    )
    d = evaluate_observation(obs)
    assert d.decision == "select_alternative_tool"


def test_unsupported_operation_without_alternative_fails():
    obs = ExecutionObservation(
        is_success=False, terminal=True, category="unsupported_operation",
        triggering_observation="Only SELECT statements are allowed",
        has_alternative=False,
    )
    d = evaluate_observation(obs)
    assert d.decision == "fail"
    assert "limitation" in d.proposed_next_action.lower()


def test_invalid_arguments_corrects_then_asks():
    obs = ExecutionObservation(
        is_success=False, category="invalid_arguments",
        requires_modified_arguments=True, attempts=0,
        triggering_observation="Invalid arguments: 'query' required",
    )
    assert evaluate_observation(obs).decision == "correct_arguments"
    exhausted = ExecutionObservation(
        is_success=False, category="invalid_arguments",
        requires_modified_arguments=True, attempts=3,
        triggering_observation="Invalid arguments: 'query' required",
    )
    assert evaluate_observation(exhausted).decision == "request_clarification"


def test_transient_retries_within_budget_then_replans():
    obs = ExecutionObservation(
        is_success=False, category="timeout", attempts=0, budget_left=True,
        triggering_observation="query timed out",
    )
    assert evaluate_observation(obs).decision == "retry"
    exhausted = ExecutionObservation(
        is_success=False, category="timeout", attempts=3, budget_left=True,
        triggering_observation="query timed out",
    )
    assert evaluate_observation(exhausted).decision == "replan"
    no_budget = ExecutionObservation(
        is_success=False, category="timeout", attempts=0, budget_left=False,
        triggering_observation="query timed out",
    )
    assert evaluate_observation(no_budget).decision == "replan"


def test_empty_result_continues_with_verification_action():
    obs = ExecutionObservation(
        is_success=True, is_empty=True, category="empty_result",
        triggering_observation="query returned 0 rows",
    )
    d = evaluate_observation(obs)
    assert d.decision == "continue"
    # The proposed action must be a verification step, never a fabricated answer.
    assert "filter" in d.proposed_next_action.lower() or "date" in d.proposed_next_action.lower()


def test_partial_success_continues_and_names_the_gap():
    obs = ExecutionObservation(
        is_success=True, is_partial=True, category="partial_result",
        triggering_observation="result truncated",
    )
    d = evaluate_observation(obs)
    assert d.decision == "continue"
    assert d.reason_code == "partial_evidence"


def test_full_success_completes():
    obs = ExecutionObservation(
        is_success=True, triggering_observation="tool succeeded",
    )
    assert evaluate_observation(obs).decision == "complete"


def test_success_with_gaps_delegates_then_replans():
    base = dict(is_success=True, missing_evidence=("cost per model",),
                triggering_observation="reviewer: cost data missing")
    d = evaluate_observation(ExecutionObservation(specialist_available=True, **base))
    assert d.decision == "delegate_specialist"
    assert d.affected_task == "cost per model"
    d2 = evaluate_observation(ExecutionObservation(specialist_available=False, **base))
    assert d2.decision == "replan"


def test_contradictions_request_peer_collaboration_or_replan():
    base = dict(is_success=True, contradictions=("A says 120, B says 90",),
                triggering_observation="reviewer: conflicting figures")
    d = evaluate_observation(ExecutionObservation(specialist_available=True, **base))
    assert d.decision == "request_peer_collaboration"
    d2 = evaluate_observation(ExecutionObservation(specialist_available=False, **base))
    assert d2.decision == "replan"


def test_failed_criteria_trigger_replan():
    obs = ExecutionObservation(
        is_success=True, failed_criteria=("per-model breakdown",),
        triggering_observation="acceptance criterion not met",
    )
    d = evaluate_observation(obs)
    assert d.decision in ("replan", "delegate_specialist")


def test_unknown_error_retries_once_then_fails_honestly():
    first = ExecutionObservation(
        is_success=False, category="unknown_error", attempts=0,
        triggering_observation="something odd",
    )
    assert evaluate_observation(first).decision == "retry"
    second = ExecutionObservation(
        is_success=False, category="unknown_error", attempts=1,
        triggering_observation="something odd",
    )
    d = evaluate_observation(second)
    assert d.decision == "fail"
    assert "unknown" in d.reason_code or "limitation" in d.proposed_next_action.lower()


# ── Structure: no chain-of-thought, continuity preserved ────────────────────
def test_decision_dict_has_no_private_reasoning():
    d = evaluate_observation(ExecutionObservation(
        is_success=True, triggering_observation="ok",
        objective="Calculate profitability by vehicle model",
    ))
    payload = d.to_dict()
    forbidden = {"thought", "reasoning", "chain_of_thought", "scratchpad", "internal"}
    assert not (forbidden & set(payload.keys()))
    # Continuity: the original objective is carried forward verbatim.
    assert payload["objective"] == "Calculate profitability by vehicle model"
    assert payload["decision"] == "complete"
    for key in ("decision", "triggering_observation", "reason_code", "proposed_next_action", "budget_impact"):
        assert key in payload


def test_budget_impact_recorded():
    d = evaluate_observation(ExecutionObservation(
        is_success=False, category="timeout", attempts=0,
        triggering_observation="timed out",
    ))
    assert d.budget_impact == "one_retry"


# ── Reviewer findings → decision (P2.3 seam) ────────────────────────────────
def test_decision_from_reviewer_findings_missing_evidence():
    d = decision_from_review(
        {"verdict": "needs_revision", "is_complete": False, "needs_evidence": True,
         "missing_evidence": ["verified cost data per model"]},
        specialist_available=True,
    )
    assert d.decision == "delegate_specialist"
    assert "cost data" in d.triggering_observation


def test_decision_from_reviewer_approved_completes():
    d = decision_from_review({"verdict": "approved", "is_complete": True})
    assert d.decision == "complete"


def test_decision_from_no_review_is_continue():
    d = decision_from_review(None)
    assert d.decision == "continue"
    assert d.reason_code == "no_review"


def test_decision_is_immutable_dataclass():
    d = ExecutionDecision(
        decision="continue", triggering_observation="x", evidence=(),
        reason_code="r", affected_task=None, proposed_next_action="p",
        budget_impact="none", requires_authorization=False,
    )
    with pytest.raises(Exception):
        d.decision = "fail"  # frozen
