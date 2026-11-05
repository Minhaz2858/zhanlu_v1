"""Stage :420 micro-fix — the data tool result must carry the ACTUAL VERIFY verdict.

The defect (reviewer-flagged, pre-existing): ``query_database_tool`` read
``getattr(result, "verification", ...)`` where ``result`` was a local of
``_run_pipeline`` — a guaranteed ``NameError`` inside the tool's assembly
path, silently swallowed by a bare ``except Exception: pass``. Consequence:
``verified`` / ``verify_detail`` in the tool result were ALWAYS ``None``,
even when the FSM's VERIFY step had durably committed a real verdict to
``executions.confidence_factors["verification"]``.

Acceptance (reviewer's words):
  1. Verified execution returns the actual verification result.
  2. Failed verification remains distinguishable from missing verification.
  3. No silent exception swallowing.
  4. No unrelated changes to tool-result semantics.

The fix reads the durable verdict where the session is still open (the
result contract, which already owns the durable-observation recovery) and
projects it onto the tool result. Deterministic doubles for failure paths;
the real test DB for the durable-row paths.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.database import SessionLocal  # noqa: E402
from app.models.execution import Execution, ObservationRecord  # noqa: E402
from app.services.db.result_contract import (  # noqa: E402
    ExecutionStatus,
    normalize_query_result,
    project_verification,
)


def _seed_execution(db, *, confidence_factors=None) -> str:
    row = Execution(
        user_message="review question",
        confidence_factors=confidence_factors,
    )
    db.add(row)
    db.flush()
    db.add(ObservationRecord(
        execution_id=row.id, plan_node_id=None, seq=0,
        observation_type="nl2sql", success=True,
        result_data={
            "sql": "SELECT 1 AS n",
            "data": [{"n": 1}],
            "source_id": "kb-verify-test",
        },
    ))
    db.commit()
    return row.id


def _cleanup(db, execution_id: str) -> None:
    try:
        db.query(ObservationRecord).filter(
            ObservationRecord.execution_id == execution_id
        ).delete(synchronize_session=False)
        db.query(Execution).filter(Execution.id == execution_id).delete(
            synchronize_session=False
        )
        db.commit()
    except Exception:  # noqa: BLE001 — cleanup must never mask a failure
        db.rollback()


# ── Acceptance 1: verified execution returns the ACTUAL verdict ──────────


def test_verified_execution_returns_actual_verification_result():
    db = SessionLocal()
    try:
        exec_id = _seed_execution(db, confidence_factors={"verification": {
            "passed": True,
            "all_passed": True,
            "checks": [
                {"check": "coverage", "ok": True, "detail": "ok",
                 "critical": True},
            ],
        }})

        normalized = normalize_query_result(
            SimpleNamespace(execution_id=exec_id), db, question="q",
        )

        assert normalized.success is True
        assert normalized.execution_status is ExecutionStatus.SUCCEEDED

        # The durable verdict is surfaced in provenance…
        verdict = normalized.provenance.get("verification")
        assert isinstance(verdict, dict), (
            "the durable VERIFY verdict was not loaded into provenance"
        )
        assert verdict.get("passed") is True

        # …and the tool-result projection returns it — not None.
        verified, detail = project_verification(normalized.provenance)
        assert verified is True, (
            "verified must reflect the ACTUAL persisted verdict, not the "
            "historically-always-None None"
        )
        assert detail == "all checks passed"
    finally:
        _cleanup(db, exec_id)


# ── Acceptance 2: failed verification ≠ missing verification ─────────────


def test_failed_verification_is_false_with_failing_check_detail():
    db = SessionLocal()
    try:
        exec_id = _seed_execution(db, confidence_factors={"verification": {
            "passed": False,
            "all_passed": False,
            "checks": [
                {"check": "observation_success", "ok": False,
                 "detail": "1 failed", "critical": True},
                {"check": "coverage", "ok": True, "detail": "ok",
                 "critical": True},
            ],
        }})

        normalized = normalize_query_result(
            SimpleNamespace(execution_id=exec_id), db, question="q",
        )
        verified, detail = project_verification(normalized.provenance)

        assert verified is False, "a failed verdict must not read as None"
        assert "observation_success: 1 failed" in (detail or ""), (
            "verify_detail must name the failing check"
        )
    finally:
        _cleanup(db, exec_id)


def test_missing_verification_is_none_never_false_pass():
    db = SessionLocal()
    try:
        exec_id = _seed_execution(db, confidence_factors=None)

        normalized = normalize_query_result(
            SimpleNamespace(execution_id=exec_id), db, question="q",
        )
        assert normalized.success is True

        # No verdict was ever produced: provenance must NOT fabricate one.
        assert "verification" not in normalized.provenance
        verified, detail = project_verification(normalized.provenance)
        assert verified is None, (
            "missing verification must stay None — distinct from "
            "verified=False (failed) and from verified=True (passed)"
        )
        assert detail is None
    finally:
        _cleanup(db, exec_id)


# ── Acceptance 3: no silent exception swallowing ─────────────────────────


class _ExecutionLookupFailureDB:
    """Real DB for observation recovery; broken ONLY for the Execution
    lookup the verification read uses. Every other attribute delegates."""

    def __init__(self, real):
        self._real = real

    def query(self, model, *a, **k):
        if getattr(model, "__name__", "") == "Execution":
            raise RuntimeError("execution lookup unavailable")
        return self._real.query(model, *a, **k)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_verification_lookup_failure_is_logged_not_swallowed(caplog):
    db = SessionLocal()
    try:
        exec_id = _seed_execution(db, confidence_factors={"verification": {
            "passed": True, "all_passed": True, "checks": [],
        }})
        broken = _ExecutionLookupFailureDB(db)

        with caplog.at_level(logging.WARNING, logger="app.services.db.result_contract"):
            normalized = normalize_query_result(
                SimpleNamespace(execution_id=exec_id), broken, question="q",
            )

        # The observation recovery path is unaffected — result still intact.
        assert normalized.success is True
        # The verdict is absent, and the absence is LOGGED, not silent.
        assert "verification" not in normalized.provenance
        assert any(
            "verification" in (r.getMessage().lower())
            for r in caplog.records
        ), "a verification lookup failure must be logged, never swallowed"

        verified, _detail = project_verification(normalized.provenance)
        assert verified is None
    finally:
        _cleanup(db, exec_id)


def test_malformed_verdict_shape_degrades_to_none():
    """A non-dict verdict (corrupt/legacy shape) is 'missing', not a pass."""
    assert project_verification({"verification": "yes"}) == (None, None)
    assert project_verification({"verification": {"checks": "oops"}}) == (
        None, None,
    )
    assert project_verification(None) == (None, None)
    assert project_verification({}) == (None, None)
