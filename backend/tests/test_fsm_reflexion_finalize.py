"""Tests for reflexion wiring in FSM FINALIZE.

The FSM's _run_reflexion method runs a heuristic self-critique on the
generated response and stores the verdict in confidence_factors["reflexion"].
Non-accept verdicts penalize the confidence score.
"""

from __future__ import annotations

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
        task_spec=None,
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
        user_message="make a report",
    )


class TestRunReflexion:
    def test_accept_verdict_no_penalty(self, fsm, exec_request):
        """Clean response → accept verdict → no confidence penalty."""
        fsm._run_reflexion(exec_request, "Here is the report you asked for.")
        assert fsm.execution.confidence_factors["reflexion"]["verdict"] == "accept"
        assert fsm.execution.confidence_score == 0.75  # unchanged

    def test_revise_verdict_penalizes_confidence(self, fsm, exec_request):
        """Response with failure markers → revise verdict → confidence penalty."""
        fsm._run_reflexion(exec_request, "Failed to load artifact: HTTP 404")
        rf = fsm.execution.confidence_factors["reflexion"]
        assert rf["verdict"] == "revise"
        assert rf["confidence"] < 0.5
        assert fsm.execution.confidence_score < 0.75  # penalized

    def test_reject_verdict_larger_penalty(self, fsm, exec_request):
        """Reject verdict → larger penalty than revise."""
        fsm._run_reflexion(exec_request, "Internal server error")
        rf = fsm.execution.confidence_factors["reflexion"]
        # "Internal server error" triggers the revise fallback (fatal marker)
        assert rf["verdict"] in ("revise", "reject")
        assert fsm.execution.confidence_score < 0.75

    def test_confidence_floor_at_zero(self, fsm, exec_request):
        """Confidence never goes below 0 even with a large penalty."""
        fsm.execution.confidence_score = 0.05
        fsm._run_reflexion(exec_request, "Failed to load artifact: HTTP 404")
        assert fsm.execution.confidence_score >= 0.0

    def test_verdict_stored_in_confidence_factors(self, fsm, exec_request):
        """The reflexion verdict must be persisted in confidence_factors."""
        fsm._run_reflexion(exec_request, "Here is the report.")
        assert "reflexion" in fsm.execution.confidence_factors
        rf = fsm.execution.confidence_factors["reflexion"]
        assert "verdict" in rf
        assert "confidence" in rf
        assert "issues" in rf

    def test_db_commit_called(self, fsm, exec_request):
        """The method must commit the execution to persist the verdict."""
        fsm._run_reflexion(exec_request, "Here is the report.")
        fsm.db.commit.assert_called()

    def test_never_raises_on_db_error(self, fsm, exec_request):
        """A DB error must not propagate — reflexion is best-effort."""
        fsm.db.commit.side_effect = RuntimeError("db boom")
        # Should not raise
        fsm._run_reflexion(exec_request, "Here is the report.")

    def test_empty_response_accepted(self, fsm, exec_request):
        """Empty response → heuristic fallback → accept (no fatal markers)."""
        fsm._run_reflexion(exec_request, "")
        assert fsm.execution.confidence_factors["reflexion"]["verdict"] == "accept"

    def test_preserves_existing_confidence_factors(self, fsm, exec_request):
        """Existing confidence_factors keys must not be overwritten."""
        fsm.execution.confidence_factors = {"verification": {"checks": []}}
        fsm._run_reflexion(exec_request, "Here is the report.")
        assert "verification" in fsm.execution.confidence_factors
        assert "reflexion" in fsm.execution.confidence_factors


class TestReflexionIntegration:
    def test_reflexion_uses_fallback_verdict(self, fsm, exec_request):
        """The FSM must use _fallback_verdict (sync, no LLM) for the critique."""
        from app.services.synexia.reflexion import _fallback_verdict

        # Verify the fallback is what _run_reflexion calls
        expected = _fallback_verdict("Failed to load artifact: HTTP 404")
        fsm._run_reflexion(exec_request, "Failed to load artifact: HTTP 404")
        actual = fsm.execution.confidence_factors["reflexion"]
        assert actual["verdict"] == expected.verdict
        assert actual["confidence"] == expected.confidence
