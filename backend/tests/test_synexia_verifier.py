"""Tests for the Synexia VERIFY validator (deterministic + LLM pass)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import sys
import types

from app.services.synexia.verifier import (
    VerificationResult,
    verify_execution,
    verify_with_llm,
)


# --- Minimal in-memory fakes (no DB) -----------------------------------------
# Avoid spinning up SQLite for these tests; the deterministic checks operate
# on plain attributes (artifact_ids, success, result_data) that the fakes
# provide. We replace the verifier's _check_artifact_exists with a stub that
# consults a class-level set, so no sys.modules pollution is needed (which
# would break app.routers.agents's import chain in cross-file test runs).


import app.services.synexia.verifier as _v  # noqa: E402


class _FakeArtifactModel:
    """Stand-in for the Artifact ORM class. Holds a class-level set of
    'existing' artifact ids that the patched artifact_exists check consults."""

    existing_ids: set[str] = set()


@dataclass
class _FakePlanNode:
    id: str = "node-1"
    name: str = "node-1"
    status: str = "completed"
    optional: bool = False
    error: Optional[str] = None


@dataclass
class _FakePlan:
    nodes: list = field(default_factory=list)


@dataclass
class _FakeObservation:
    id: str = "obs-1"
    plan_node: Optional[_FakePlanNode] = None
    observation_type: str = "tool_call"
    tool_name: str = "some_tool"
    request_args: Optional[dict] = None
    success: bool = True
    result_data: Optional[dict] = None
    error_message: Optional[str] = None
    artifact_ids: list = field(default_factory=list)


@dataclass
class _FakeExecution:
    id: str = "exec-1"
    observations: list = field(default_factory=list)
    confidence_factors: Optional[dict] = None


def _make_fake_db(existing_artifact_ids: set[str] | None = None):
    """Configure the class-level existing_ids set and return a placeholder db.
    The patched _check_artifact_exists reads _FakeArtifactModel.existing_ids
    directly, so the db argument is unused."""
    _FakeArtifactModel.existing_ids = existing_artifact_ids or set()
    return types.SimpleNamespace()


# Replace the verifier's artifact_exists check with our stub. It does NOT
# import app.models.artifact, so it cannot pollute sys.modules.
# Signature mirrors the real _check_artifact_exists(db, observations) — it
# receives the (plan-scoped) observation list, not the execution.
def _patched_check_artifact_exists(db, observations):
    missing = []
    for obs in observations or []:
        for aid in (obs.artifact_ids or []):
            if aid not in _FakeArtifactModel.existing_ids:
                missing.append(aid)
    return {
        "check": "artifact_exists",
        "ok": not missing,
        "detail": f"{len(missing)} missing" if missing else "ok",
        "critical": True,
    }


_v._check_artifact_exists = _patched_check_artifact_exists


# --- Tests ------------------------------------------------------------------


def test_verify_passes_clean_execution():
    """A clean execution with successful observations and no artifacts passes."""
    db = _make_fake_db(existing_artifact_ids=set())
    exec_ = _FakeExecution(
        observations=[
            _FakeObservation(success=True, result_data={"value": 1}),
            _FakeObservation(success=True, result_data={"value": 2}),
        ]
    )
    r = verify_execution(db, exec_, plan=_FakePlan())
    assert r.passed is True
    assert r.artifact_ok and r.observations_ok and r.data_integrity_ok
    assert r.all_checks_passed() is True
    assert any(c["check"] == "artifact_exists" and c["ok"] for c in r.checks)


def test_verify_flags_missing_artifact():
    """When an observation references an artifact id that has no row, fail."""
    db = _make_fake_db(existing_artifact_ids={"a1"})
    exec_ = _FakeExecution(
        observations=[
            _FakeObservation(success=True, result_data={"x": 1}, artifact_ids=["a1", "a2"]),
        ]
    )
    r = verify_execution(db, exec_, plan=_FakePlan())
    assert r.passed is False
    assert any(c["check"] == "artifact_exists" and not c["ok"] for c in r.checks)


def test_verify_flags_failed_required_observation():
    """A failed observation with a required (non-optional) node fails."""
    node = _FakePlanNode(optional=False)
    exec_ = _FakeExecution(
        observations=[
            _FakeObservation(plan_node=node, success=False, error_message="boom"),
        ]
    )
    r = verify_execution(_make_fake_db(), exec_, plan=_FakePlan())
    assert r.passed is False
    assert any(c["check"] == "observation_success" and not c["ok"] for c in r.checks)


def test_verify_ignores_failed_optional_observation():
    """A failed observation on an optional node is tolerated."""
    node = _FakePlanNode(optional=True)
    exec_ = _FakeExecution(
        observations=[
            _FakeObservation(plan_node=node, success=False, error_message="ignored"),
        ]
    )
    r = verify_execution(_make_fake_db(), exec_, plan=_FakePlan())
    assert r.passed is True
    assert r.observations_ok is True


def test_verify_flags_empty_result_data():
    """A successful observation with empty result_data fails data_integrity."""
    exec_ = _FakeExecution(
        observations=[
            _FakeObservation(success=True, result_data=None),
        ]
    )
    r = verify_execution(_make_fake_db(), exec_, plan=_FakePlan())
    assert r.passed is False
    assert any(c["check"] == "data_integrity" and not c["ok"] for c in r.checks)


def test_verify_with_llm_short_circuits_when_flag_off(monkeypatch):
    """When SYNEXIA_VERIFIER_LLM_ENABLED is False (default), skip the LLM call."""
    from app.config import settings as _settings
    assert _settings.SYNEXIA_VERIFIER_LLM_ENABLED is False

    exec_ = _FakeExecution(observations=[_FakeObservation(success=True, result_data={"x": 1})])
    r = VerificationResult([{"check": "artifact_exists", "ok": True, "detail": "ok"}])
    called = []

    def _llm(_prompt):
        called.append(1)
        return {"checks": [{"name": "rubric", "ok": True, "detail": "ok"}]}

    out = verify_with_llm(exec_, r, llm_callable=_llm)
    assert out == []
    assert called == []  # never invoked because flag is off


def test_verify_with_llm_runs_when_flag_on(monkeypatch):
    """When the flag is on, llm_callable is invoked and its checks returned."""
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "SYNEXIA_VERIFIER_LLM_ENABLED", True)

    exec_ = _FakeExecution(observations=[_FakeObservation(success=True, result_data={"x": 1})])
    r = VerificationResult([{"check": "artifact_exists", "ok": True, "detail": "ok"}])
    captured = []

    def _llm(prompt):
        captured.append(prompt)
        return {"checks": [{"name": "rubric", "ok": False, "detail": "weak"}]}

    out = verify_with_llm(exec_, r, llm_callable=_llm)
    assert len(captured) == 1
    assert len(out) == 1
    assert out[0]["check"] == "rubric"
    assert out[0]["ok"] is False

    # The FSM's _run_verify extends result.checks with the LLM pass.
    r.checks.extend(out)
    # all_checks_passed should now be False
    assert r.all_checks_passed() is False  # the deterministic check is ok but LLM is not


def test_verify_with_llm_short_circuits_on_bad_response(monkeypatch):
    """An LLM that returns malformed output is treated as no-op (non-fatal)."""
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "SYNEXIA_VERIFIER_LLM_ENABLED", True)

    exec_ = _FakeExecution(observations=[_FakeObservation(success=True, result_data={"x": 1})])
    r = VerificationResult([{"check": "artifact_exists", "ok": True, "detail": "ok"}])

    def _llm(_p):
        return {"checks": "not-a-list"}  # malformed

    out = verify_with_llm(exec_, r, llm_callable=_llm)
    assert out == []
