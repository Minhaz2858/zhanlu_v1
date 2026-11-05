"""Phase 2.1 — shared failure taxonomy + bounded healing policy (TDD).

The SINGLE classification authority for tool-call healing: typed result fields
win, then exception type, then the existing string markers (reused, not copied).
Empty results are NOT failures; permission failures are terminal; mutating
tools are never blindly retried.

Run: pytest tests/test_execution_recovery.py -q
"""
import asyncio
import json

import pytest

from app.services.execution_recovery import (
    EXEC_RECOVERY_MAX_PER_TASK_DEFAULT,
    FailureCategory,
    FailureClassification,
    FailureOutcome,
    HealingBudget,
    RecoveryPolicy,
    classify_failure,
    should_retry,
    should_reformulate,
)


# ── Taxonomy completeness ────────────────────────────────────────────────────
def test_failure_category_taxonomy_complete():
    required = {
        "invalid_arguments", "schema_validation_error", "missing_required_parameter",
        "permission_denied", "resource_not_found", "resource_unavailable",
        "connection_error", "timeout", "rate_limit", "provider_error",
        "database_error", "empty_result", "malformed_output", "partial_result",
        "unsupported_operation", "unknown_error",
    }
    assert required == {c.value for c in FailureCategory}


def test_failure_outcomes_distinguish_success_from_failure():
    # The six-way distinction the mission demands — zero rows is NOT a failure.
    assert {o.value for o in FailureOutcome} == {
        "success", "success_with_empty_result", "success_with_partial_data",
        "execution_failure", "authorization_failure", "invalid_request",
        "temporary_infrastructure_failure",
    }


# ── Precedence: typed result fields ──────────────────────────────────────────
def test_empty_result_is_success_not_failure():
    c = classify_failure({"success": True, "rows": []})
    assert c.outcome == FailureOutcome.success_with_empty_result
    assert c.category == FailureCategory.empty_result
    assert c.retryable is False
    assert "filter" in c.recovery_hint or "date" in c.recovery_hint


def test_empty_set_kind_is_success_not_failure():
    c = classify_failure({"success": True, "error_kind": "empty_set", "rows": []})
    assert c.outcome == FailureOutcome.success_with_empty_result
    assert c.retryable is False


def test_partial_success_is_incomplete_not_failed():
    c = classify_failure({"success": True, "truncated": True, "rows": [["a"]]})
    assert c.outcome == FailureOutcome.success_with_partial_data
    assert c.category == FailureCategory.partial_result
    assert c.retryable is False


def test_full_success_classified_plain():
    c = classify_failure({"success": True, "rows": [["a"]], "columns": ["x"]})
    assert c.outcome == FailureOutcome.success
    assert c.retryable is False


def test_typed_failure_kind_wins_over_markers():
    # A typed non-retryable kind beats a transient-sounding message.
    c = classify_failure({
        "success": False, "failure_kind": "result_extraction_failure",
        "error": "connection timeout while extracting",
    })
    assert c.category == FailureCategory.malformed_output
    assert c.retryable is False


def test_error_kind_tags_map_to_categories():
    cases = {
        "driver_missing": FailureCategory.provider_error,
        "turn_sql_budget_exhausted": FailureCategory.rate_limit,
        "source_verification_refused": FailureCategory.permission_denied,
        "source_verification_clarify": FailureCategory.missing_required_parameter,
    }
    for kind, expected in cases.items():
        c = classify_failure({"success": False, "error_kind": kind, "error": "x"})
        assert c.category == expected, kind


def test_approval_pending_is_authorization_not_retry():
    c = classify_failure({"success": False, "requires_approval": True})
    assert c.outcome == FailureOutcome.authorization_failure
    assert c.terminal is True and c.retryable is False
    assert "approval" in c.recovery_hint.lower()


# ── String markers (reused semantics) ────────────────────────────────────────
def test_permission_denied_is_terminal():
    c = classify_failure({"success": False, "error": "Permission denied: table finances"})
    assert c.category == FailureCategory.permission_denied
    assert c.terminal is True and c.retryable is False
    assert c.outcome == FailureOutcome.authorization_failure


def test_not_found_maps_to_resource_not_found():
    c = classify_failure({"success": False, "error": "table v_missing not found"})
    assert c.category == FailureCategory.resource_not_found
    assert c.retryable is False  # retrying unchanged is deterministic-futile


def test_timeout_is_retryable_transient():
    c = classify_failure({"success": False, "error": "query timed out after 30s"})
    assert c.category == FailureCategory.timeout
    assert c.retryable is True and c.terminal is False
    assert c.outcome == FailureOutcome.temporary_infrastructure_failure


def test_connection_error_is_retryable_transient():
    c = classify_failure({"success": False, "error": "connection refused (111)"})
    assert c.category == FailureCategory.connection_error
    assert c.retryable is True


def test_rate_limit_classified_with_backoff_hint():
    c = classify_failure({"success": False, "error": "429 Too Many Requests"})
    assert c.category == FailureCategory.rate_limit
    assert c.retryable is True
    assert "backoff" in c.recovery_hint.lower() or "retry-after" in c.recovery_hint.lower()


def test_invalid_arguments_requires_modified_retry():
    c = classify_failure({
        "success": False, "error": "Invalid arguments: 'query' is a required property",
    })
    assert c.category in (FailureCategory.invalid_arguments, FailureCategory.missing_required_parameter)
    assert c.requires_modified_arguments is True
    assert c.retryable is False  # deterministic failure — identical args must not re-run


def test_malformed_output_classified():
    c = classify_failure({"success": False, "error": "malformed output: failed to parse JSON"})
    assert c.category == FailureCategory.malformed_output


def test_sql_policy_denial_is_unsupported_operation():
    c = classify_failure({"success": False, "error": "Only SELECT statements are allowed, got: INSERT"})
    assert c.category == FailureCategory.unsupported_operation
    assert c.retryable is False and c.terminal is True


# ── Exception path ───────────────────────────────────────────────────────────
def test_timeout_exception_classified():
    c = classify_failure(error=TimeoutError("tool exceeded deadline"))
    assert c.category == FailureCategory.timeout and c.retryable is True


def test_permission_exception_terminal():
    c = classify_failure(error=PermissionError("denied"))
    assert c.category == FailureCategory.permission_denied and c.terminal is True


def test_json_decode_error_is_malformed():
    try:
        json.loads("{not json")
    except json.JSONDecodeError as e:
        c = classify_failure(error=e)
    assert c.category == FailureCategory.malformed_output


def test_unknown_error_default():
    c = classify_failure({"success": False, "error": "something odd happened"})
    assert c.category == FailureCategory.unknown_error
    assert c.outcome == FailureOutcome.execution_failure


def test_unknown_error_on_mutating_tool_is_not_retryable():
    # G3: without idempotency evidence an unknown failure on a WRITE must not
    # be retried — the write may have landed.
    c = classify_failure({"success": False, "error": "something odd"}, tool_name="create_po_draft", mutating=True)
    assert c.category == FailureCategory.unknown_error
    assert c.retryable is False


# ── Bounded healing policy ───────────────────────────────────────────────────
def _cls(**kw):
    return classify_failure({"success": False, **kw})


def test_should_retry_transient_within_budget():
    c = _cls(error="connection reset")
    assert should_retry(c, attempts=0, max_attempts=2) is True
    assert should_retry(c, attempts=2, max_attempts=2) is False


def test_never_retry_permission_or_terminal():
    c = classify_failure({"success": False, "error": "permission denied"})
    assert should_retry(c, attempts=0, max_attempts=3) is False


def test_invalid_arguments_never_plain_retried():
    c = _cls(error="Invalid arguments: bad type")
    assert should_retry(c, attempts=0, max_attempts=3) is False


def test_mutating_requires_idempotency():
    c = _cls(error="connection reset")  # otherwise transient-retryable
    assert should_retry(c, attempts=0, max_attempts=3, mutating=True, idempotent=False) is False
    assert should_retry(c, attempts=0, max_attempts=3, mutating=True, idempotent=True) is True


def test_task_healing_budget_gates_retry():
    c = _cls(error="timed out")
    assert should_retry(c, attempts=0, max_attempts=2, task_healing_budget_left=False) is False


def test_reformulate_only_for_arg_fixable_categories():
    assert should_reformulate(_cls(error="Invalid arguments: 'x' required")) is True
    assert should_reformulate(_cls(error="'table' is a required property")) is True
    assert should_reformulate(_cls(error="permission denied")) is False
    assert should_reformulate(_cls(error="connection refused")) is False
    assert should_reformulate(_cls(error="query timed out")) is False


def test_recovery_policy_from_settings():
    policy = RecoveryPolicy.from_settings()
    assert policy.max_attempts_per_call >= 1
    assert policy.max_healing_per_task == EXEC_RECOVERY_MAX_PER_TASK_DEFAULT


def test_healing_budget_bounded_and_honest():
    budget = HealingBudget(max_healings=2)
    assert budget.consume("create_po_draft") is True
    assert budget.consume("create_po_draft") is True
    assert budget.consume("create_po_draft") is False
    assert budget.remaining == 0
    assert HealingBudget(max_healings=2).attempts("x") == 0


def test_default_max_healing_per_task_is_bounded():
    # Mission: no endless loops — the whole task heals a bounded number of times.
    assert 1 <= EXEC_RECOVERY_MAX_PER_TASK_DEFAULT <= 10
