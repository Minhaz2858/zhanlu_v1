"""Integration tests for the QUALITY_EVAL phase wired into the FSM.

The FSM's ``run_quality_eval`` method runs a combined completeness +
reflexion LLM critique (with a bounded corrective re-generation loop) on
the generated response.  It stores the verdict in
``confidence_factors["quality_eval"]`` and penalizes confidence for
non-accept verdicts — same convention as ``_run_reflexion``.

``call_llm`` is patched at the source (``app.services.llm_service.call_llm``)
because ``run_quality_eval`` bridges to it via an injected ``_llm_call``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.synexia.fsm import ExecutionRequest, SynexiaFSM


@pytest.fixture()
def fsm():
    """Create a SynexiaFSM with a mock DB and a fake execution."""
    f = SynexiaFSM.__new__(SynexiaFSM)
    f.db = MagicMock()
    f.execution = SimpleNamespace(
        id="exec-test",
        observations=None,
        task_spec={"acceptance_criteria": ["include revenue figures"]},
        context_manifest=None,
        confidence_factors={},
        confidence_score=0.75,
        current_state="finalize",
    )
    f.plan = None
    f._last_verify_result = None
    return f


@pytest.fixture()
def exec_request():
    return ExecutionRequest(
        conversation_id="conv-test",
        agent_name="general_assistant",
        user_message="make a sales report",
    )


def _accept_json():
    return {"response": json.dumps({
        "verdict": "accept",
        "completeness_score": 0.9,
        "confidence": 0.9,
    })}


def _revise_json():
    return {"response": json.dumps({
        "verdict": "revise",
        "completeness_score": 0.3,
        "confidence": 0.4,
        "issues": ["revenue figures missing"],
        "suggestions": ["add a revenue table"],
    })}


class TestRunQualityEval:
    def test_accept_verdict_no_penalty(self, fsm, exec_request, monkeypatch):
        """Clean accept → no confidence penalty, verdict stored."""
        monkeypatch.setattr("app.services.llm_service.call_llm", lambda **kw: _accept_json())
        qer = fsm.run_quality_eval(exec_request, "Here is the report with revenue.")
        assert qer is not None
        assert qer.verdict == "accept"
        assert qer.is_ok is True
        assert fsm.execution.confidence_score == 0.75  # unchanged
        assert "quality_eval" in fsm.execution.confidence_factors

    def test_revise_penalizes_confidence(self, fsm, exec_request, monkeypatch):
        """Loop exhausts at revise → final revise verdict → confidence penalized by 0.08."""
        # Every eval returns revise; regeneration produces text but eval
        # stays revise → iterations exhausted, final verdict = revise.
        monkeypatch.setattr("app.services.llm_service.call_llm", lambda **kw: _revise_json())
        qer = fsm.run_quality_eval(exec_request, "Here is the report.")
        assert qer is not None
        assert qer.verdict == "revise"
        assert fsm.execution.confidence_score < 0.75  # penalized by revise (0.08)

    def test_corrective_loop_revises_text(self, fsm, exec_request, monkeypatch):
        """First eval revise → regenerate → second eval accept; final_text revised."""
        calls = iter([_revise_json(), {"response": "Here is the revised report with revenue figures."}, _accept_json()])
        monkeypatch.setattr("app.services.llm_service.call_llm", lambda **kw: next(calls))
        qer = fsm.run_quality_eval(exec_request, "Here is the report.")
        assert qer is not None
        assert qer.iterations == 1
        assert "revenue figures" in qer.final_text

    def test_disabled_returns_none(self, fsm, exec_request, monkeypatch):
        """When SYNEXIA_QUALITY_EVAL_ENABLED=False, returns None (no eval)."""
        monkeypatch.setattr("app.config.settings.SYNEXIA_QUALITY_EVAL_ENABLED", False)
        qer = fsm.run_quality_eval(exec_request, "Here is the report.")
        assert qer is None

    def test_llm_failure_heuristic_fallback(self, fsm, exec_request, monkeypatch):
        """A raising call_llm falls back to the heuristic verdict (non-fatal).

        evaluate_quality catches the exception internally and returns the
        heuristic fallback, so run_quality_eval returns a result (not None).
        """
        def boom(**kw):
            raise RuntimeError("LLM down")
        monkeypatch.setattr("app.services.llm_service.call_llm", boom)
        qer = fsm.run_quality_eval(exec_request, "Here is the report.")
        assert qer is not None
        # Heuristic fallback for clean text → accept.
        assert qer.verdict == "accept"
        assert qer.raw == "heuristic-only"

    def test_no_state_transition_without_callback(self, fsm, exec_request, monkeypatch):
        """on_state_change=None → no QUALITY_EVAL state transition emitted."""
        monkeypatch.setattr("app.services.llm_service.call_llm", lambda **kw: _accept_json())
        # Should not raise even though _on_state is not provided.
        qer = fsm.run_quality_eval(exec_request, "Here is the report.", on_state_change=None)
        assert qer is not None

    def test_quality_eval_stored_in_confidence_factors(self, fsm, exec_request, monkeypatch):
        """The verdict must be persisted in confidence_factors["quality_eval"]."""
        monkeypatch.setattr("app.services.llm_service.call_llm", lambda **kw: _accept_json())
        fsm.run_quality_eval(exec_request, "Here is the report.")
        qe = fsm.execution.confidence_factors["quality_eval"]
        assert qe["verdict"] == "accept"
        assert qe["completeness_score"] == pytest.approx(0.9)
        assert qe["is_ok"] is True

    def test_preserves_existing_confidence_factors(self, fsm, exec_request, monkeypatch):
        """Existing confidence_factors keys must not be overwritten."""
        monkeypatch.setattr("app.services.llm_service.call_llm", lambda **kw: _accept_json())
        fsm.execution.confidence_factors = {"verification": {"checks": []}}
        fsm.run_quality_eval(exec_request, "Here is the report.")
        assert "verification" in fsm.execution.confidence_factors
        assert "quality_eval" in fsm.execution.confidence_factors

    def test_db_commit_called(self, fsm, exec_request, monkeypatch):
        """The method must commit to persist the verdict."""
        monkeypatch.setattr("app.services.llm_service.call_llm", lambda **kw: _accept_json())
        fsm.run_quality_eval(exec_request, "Here is the report.")
        fsm.db.commit.assert_called()

    def test_reject_larger_penalty_than_revise(self, fsm, exec_request, monkeypatch):
        """Reject verdict → 0.15 penalty (> revise's 0.08)."""
        reject_json = {"response": json.dumps({
            "verdict": "reject",
            "completeness_score": 0.1,
            "confidence": 0.1,
        })}
        # Always reject → exhausts iterations, stays reject.
        monkeypatch.setattr("app.services.llm_service.call_llm", lambda **kw: reject_json)
        qer = fsm.run_quality_eval(exec_request, "I cannot do that.")
        assert qer is not None
        assert qer.verdict == "reject"
        # 0.75 - 0.15 = 0.60
        assert fsm.execution.confidence_score == pytest.approx(0.60, abs=0.01)
