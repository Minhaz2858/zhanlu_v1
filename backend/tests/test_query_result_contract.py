"""Regression tests for the query result contract (2026-10-27).

These cover the P0 correctness gate from
``docs/plans/2026-10-27-query-result-contract-design.md``:

  RC-1  ``ExecutionResult`` has no ``observations`` attribute, so
        ``query_database`` reported "no query executed" while SQL had
        actually run and returned rows.
  RC-2  ``elapsed_ms`` vs ``duration_ms`` key mismatch → timing always NULL.
  RC-3  Unknown errors defaulted to retryable → full NL2SQL re-execution.

Tests 1-4 and 7-8 are the P0 gate. Deterministic doubles only — no live DB,
no network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.db.result_contract import (
    NON_RETRYABLE_FAILURE_KINDS,
    ExecutionStatus,
    FailureKind,
    NormalizedQueryResult,
    ResultStatus,
    extract_observations,
    normalize_query_result,
)


# ── Doubles ──────────────────────────────────────────────────────────────


def _obs(seq=0, *, otype="nl2sql", result_data=None, success=True, error=None):
    return SimpleNamespace(
        seq=seq,
        observation_type=otype,
        result_data=result_data,
        success=success,
        error_message=error,
        duration_ms=None,
    )


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    """Returns the given observations for any ``observation_records`` query."""

    def __init__(self, rows):
        self._rows = rows
        self.queried = False

    def query(self, *a, **k):
        self.queried = True
        return _FakeQuery(self._rows)


def _fsm_result(execution_id="exec-1"):
    """The REAL shape: an ExecutionResult with no observations/execution."""
    return SimpleNamespace(execution_id=execution_id)


# ── Test 1: SQL executed, rows present, observations empty on the object ──
def test_1_executed_with_rows_is_success_and_does_not_reexecute():
    """The exact original bug. ExecutionResult carries no observations; the
    rows live in observation_records. Must be a success with no re-run."""
    db = _FakeDB(
        [
            _obs(
                0,
                result_data={
                    "sql": "SELECT SUM(FAMOUNT) FROM erp_v_sale_orderentry",
                    "data": [{"total": 1234}, {"total": 99}],
                    "source_id": "kb-1",
                    "source_name": "aipdp_data_warehouse_prod",
                },
            )
        ]
    )

    # Precondition: the object genuinely has NO observations attribute.
    result = _fsm_result()
    assert not hasattr(result, "observations")

    normalized = normalize_query_result(result, db, question="q")

    assert normalized.success is True
    assert normalized.execution_status is ExecutionStatus.SUCCEEDED
    assert normalized.result_status is ResultStatus.ASSEMBLED
    assert normalized.row_count == 2
    assert normalized.failure_kind is FailureKind.NONE
    assert db.queried is True, "recovery must read the durable records"
    # Recovery preserved provenance.
    assert normalized.provenance["execution_id"] == "exec-1"


# ── Test 2: SQL executed but extraction impossible → typed, no re-exec ────
def test_2_extraction_failure_is_typed_and_non_retryable():
    db = _FakeDB([])  # no records recoverable
    normalized = normalize_query_result(_fsm_result(), db, question="q")

    assert normalized.success is False
    assert normalized.result_status is ResultStatus.EXTRACTION_FAILED
    assert normalized.failure_kind is FailureKind.RESULT_EXTRACTION_FAILURE

    tool = normalized.to_tool_result()
    assert tool["failure_kind"] == "result_extraction_failure"
    # Typed kind must stop the reliability layer from re-running the pipeline.
    assert tool["failure_kind"] in NON_RETRYABLE_FAILURE_KINDS


def test_2b_missing_execution_id_is_extraction_failure_not_silent_empty():
    normalized = normalize_query_result(
        SimpleNamespace(execution_id=None), _FakeDB([]), question="q"
    )
    assert normalized.success is False
    assert normalized.failure_kind is FailureKind.RESULT_EXTRACTION_FAILURE


# ── Test 3: genuinely invalid binding → permanent, never retried ──────────
def test_3_binding_failure_is_permanent():
    db = _FakeDB(
        [
            _obs(
                0,
                success=False,
                error=(
                    "The NL2SQL pipeline did not execute any query — no bound "
                    "data source reached the pipeline (bound_kb_ids=[])."
                ),
            )
        ]
    )
    normalized = normalize_query_result(_fsm_result(), db, question="q")

    assert normalized.success is False
    assert normalized.execution_status is ExecutionStatus.NOT_EXECUTED
    assert normalized.failure_kind is FailureKind.PERMANENT_BINDING_FAILURE
    assert "permanent_binding_failure" in NON_RETRYABLE_FAILURE_KINDS


def test_3b_generated_but_unexecuted_sql_is_a_binding_failure():
    """SQL text with no 'data' key was generated, never executed."""
    db = _FakeDB([_obs(0, result_data={"sql": "SELECT 1", "executed": False})])
    normalized = normalize_query_result(_fsm_result(), db, question="q")
    assert normalized.success is False
    assert normalized.failure_kind is FailureKind.PERMANENT_BINDING_FAILURE
    # Must never claim rows for SQL that never ran.
    assert normalized.rows == []


# ── Test 4: connection timeout → transient → bounded retry allowed ────────
def test_4_connection_timeout_is_transient_retryable():
    db = _FakeDB(
        [_obs(0, success=False, error="(2003, \"Can't connect to MySQL "
                                      "server on 'wh.local' (timed out)\")")]
    )
    normalized = normalize_query_result(_fsm_result(), db, question="q")

    assert normalized.success is False
    assert normalized.failure_kind is FailureKind.TRANSIENT_EXECUTION_FAILURE
    # The ONE class that legitimately warrants a bounded retry.
    assert "transient_execution_failure" not in NON_RETRYABLE_FAILURE_KINDS


# ── Test 7: valid execution, zero rows → SUCCESS ─────────────────────────
def test_7_zero_rows_is_success_not_execution_failure():
    db = _FakeDB(
        [_obs(0, result_data={"sql": "SELECT 1 WHERE 1=0", "data": []})]
    )
    normalized = normalize_query_result(_fsm_result(), db, question="q")

    assert normalized.success is True
    assert normalized.execution_status is ExecutionStatus.SUCCEEDED
    assert normalized.result_status is ResultStatus.EMPTY
    assert normalized.row_count == 0


# ── Test 8: truncated/incomplete → success, completeness separate ────────
def test_8_truncated_result_is_success_with_complete_false():
    db = _FakeDB(
        [
            _obs(
                0,
                result_data={
                    "sql": "SELECT * FROM big",
                    "data": [{"a": 1}],
                    "truncated": True,
                },
            )
        ]
    )
    normalized = normalize_query_result(_fsm_result(), db, question="q")

    assert normalized.success is True, "a truncated real result is still a success"
    assert normalized.complete is False
    assert any("truncated" in w.lower() for w in normalized.warnings)


# ── Contract invariants (A1): success cannot be asserted ─────────────────
def test_success_is_computed_and_not_assignable():
    n = NormalizedQueryResult(
        execution_status=ExecutionStatus.NOT_EXECUTED,
        result_status=ResultStatus.EXTRACTION_FAILED,
        failure_kind=FailureKind.PERMANENT_BINDING_FAILURE,
    )
    with pytest.raises(Exception):
        n.success = True  # type: ignore[misc]  # frozen + property


def test_contradictory_success_is_unrepresentable():
    """Execution failed ⇒ success is False regardless of result_status."""
    n = NormalizedQueryResult(
        execution_status=ExecutionStatus.FAILED,
        result_status=ResultStatus.ASSEMBLED,
        rows=[{"a": 1}],
    )
    assert n.success is False


# ── RC-2: timing key mismatch ────────────────────────────────────────────
def test_rc2_elapsed_ms_is_read_as_duration():
    from app.services.db.nl_answer_service import _elapsed_ms

    assert _elapsed_ms({"elapsed_ms": 1234.5}) == 1234.5
    # Back-compat if either side renames.
    assert _elapsed_ms({"duration_ms": 7}) == 7.0
    assert _elapsed_ms({}) is None
    assert _elapsed_ms(None) is None


# ── RC-3: typed classification wins over wording ─────────────────────────
def test_rc3_typed_kind_overrides_retryable_default():
    from app.services.agent_tools import _is_retryable

    extraction = {
        "success": False,
        "error": "some unknown wording that matches no marker",
        "failure_kind": "result_extraction_failure",
    }
    binding = {
        "success": False,
        "error": "some unknown wording",
        "failure_kind": "permanent_binding_failure",
    }
    # Unknown wording alone defaults to retryable...
    assert _is_retryable({"success": False, "error": "zzz unknown"}) is True
    # ...but a typed non-retryable kind overrides it.
    assert _is_retryable(extraction) is False
    assert _is_retryable(binding) is False


# ── Extraction helper behaviour ──────────────────────────────────────────
def test_extract_prefers_own_observations_when_present():
    own = _obs(0, result_data={"sql": "s", "data": [{"x": 1}]})
    result = SimpleNamespace(execution_id="e", observations=[own])
    db = _FakeDB([])
    obs, eid = extract_observations(result, db)
    assert obs == [own]
    assert eid == "e"
    assert db.queried is False


def test_extract_never_fabricates_when_lookup_raises():
    class Boom:
        def query(self, *a, **k):
            raise RuntimeError("db down")

    obs, eid = extract_observations(_fsm_result(), Boom())
    assert obs == []
    assert eid == "exec-1"
    # Caller must treat empty + execution id as extraction failure.
    normalized = normalize_query_result(_fsm_result(), _FakeDB([]), question="q")
    assert normalized.success is False
