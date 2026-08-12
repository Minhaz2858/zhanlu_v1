"""Tests for the Phase B artifact quality gate.

Covers:
1. ``quality_gate_decision`` — the pure decision function.
2. FSM ``_run_finalize`` gate wiring — low-confidence runs hold artifacts
   back from the shipped ExecutionResult and notify the user.
3. ``grade_quality_gate`` — the eval-harness grader.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.synexia.confidence_scorer import quality_gate_decision
from app.services.synexia.eval_harness import grade_quality_gate
from app.services.synexia.fsm import ExecutionRequest, SynexiaFSM


# ── 1. quality_gate_decision ─────────────────────────────────────────────

class TestQualityGateDecision:
    def test_disabled_always_passes(self):
        d = quality_gate_decision(0.0, ["a1"], enabled=False)
        assert d["passed"] is True
        assert d["enabled"] is False

    def test_no_artifacts_passes(self):
        d = quality_gate_decision(0.0, [], threshold=0.4)
        assert d["passed"] is True
        assert d["reason"] == "no_artifacts"

    def test_confidence_above_threshold_passes(self):
        d = quality_gate_decision(0.75, ["a1", "a2"], threshold=0.4)
        assert d["passed"] is True
        assert d["confidence"] == 0.75
        assert d["threshold"] == 0.4
        assert d["artifact_count"] == 2
        assert "held_artifact_ids" not in d

    def test_confidence_at_threshold_passes(self):
        d = quality_gate_decision(0.4, ["a1"], threshold=0.4)
        assert d["passed"] is True

    def test_confidence_below_threshold_holds(self):
        d = quality_gate_decision(0.2, ["a1", "a2"], threshold=0.4)
        assert d["passed"] is False
        assert d["reason"] == "confidence_below_threshold"
        assert d["held_artifact_ids"] == ["a1", "a2"]
        assert d["artifact_count"] == 2

    def test_held_ids_are_copied(self):
        ids = ["a1"]
        d = quality_gate_decision(0.1, ids, threshold=0.4)
        ids.append("mutated")
        assert d["held_artifact_ids"] == ["a1"]


# ── 2. FSM _run_finalize wiring ──────────────────────────────────────────

@pytest.fixture()
def fsm():
    f = SynexiaFSM.__new__(SynexiaFSM)
    f.db = MagicMock()
    obs = SimpleNamespace(
        observation_type="tool_call",
        tool_name="create_artifact",
        request_args={"title": "Report"},
        result_data={"success": True},
        success=True,
        artifact_ids=["art-1"],
    )
    f.execution = SimpleNamespace(
        id="exec-gate",
        observations=[obs],
        task_spec={"user_signal": "default"},
        context_manifest=None,
        confidence_factors={},
        confidence_score=0.0,
        current_state="finalize",
        assistant_content="",
        completed_at=None,
    )
    f.plan = None
    f._last_verify_result = None
    # Skip the LLM response generation.
    f._generate_response = lambda request: "Here is your report."
    # Skip QUALITY_EVAL (Tier 2) — these tests isolate the quality GATE,
    # not the semantic critique loop. QUALITY_EVAL is covered by
    # test_fsm_quality_eval.py.
    f.run_quality_eval = lambda *a, **kw: None
    return f


@pytest.fixture()
def exec_request():
    return ExecutionRequest(
        conversation_id="conv-gate",
        agent_name="general_assistant",
        user_message="make a report",
    )


class TestFinalizeQualityGate:
    def _patch_confidence(self, monkeypatch, score):
        import app.services.synexia.confidence_scorer as cs
        monkeypatch.setattr(
            cs, "compute_confidence",
            lambda execution, plan: (score, {"stub": True}),
        )

    def test_low_confidence_holds_artifacts(self, fsm, exec_request, monkeypatch):
        self._patch_confidence(monkeypatch, 0.2)
        result = fsm._run_finalize(exec_request)
        assert result.quality_gate is not None
        assert result.quality_gate["passed"] is False
        assert result.artifact_ids == []
        assert result.file_exports == {}
        assert result.export_artifact_id is None
        assert result.quality_gate["held_artifact_ids"] == ["art-1"]
        assert "Quality gate" in result.assistant_content
        assert result.confidence_factors["quality_gate"]["passed"] is False

    def test_high_confidence_ships_artifacts(self, fsm, exec_request, monkeypatch):
        self._patch_confidence(monkeypatch, 0.9)
        result = fsm._run_finalize(exec_request)
        assert result.quality_gate is not None
        assert result.quality_gate["passed"] is True
        assert result.artifact_ids == ["art-1"]
        assert "Quality gate" not in result.assistant_content

    def test_gate_disabled_ships_regardless(self, fsm, exec_request, monkeypatch):
        self._patch_confidence(monkeypatch, 0.1)
        monkeypatch.setattr(
            "app.services.synexia.fsm.settings.SYNEXIA_QUALITY_GATE_ENABLED",
            False,
        )
        result = fsm._run_finalize(exec_request)
        assert result.artifact_ids == ["art-1"]
        assert result.quality_gate["passed"] is True
        assert result.quality_gate["enabled"] is False

    def test_no_artifacts_no_hold(self, fsm, exec_request, monkeypatch):
        self._patch_confidence(monkeypatch, 0.1)
        fsm.execution.observations[0].artifact_ids = None
        result = fsm._run_finalize(exec_request)
        assert result.artifact_ids == []
        # Gate never engages when there is nothing to ship.
        assert result.quality_gate is None
        assert "Quality gate" not in result.assistant_content

    def test_gate_threshold_from_settings(self, fsm, exec_request, monkeypatch):
        self._patch_confidence(monkeypatch, 0.55)
        monkeypatch.setattr(
            "app.services.synexia.fsm.settings.SYNEXIA_QUALITY_GATE_THRESHOLD",
            0.6,
        )
        result = fsm._run_finalize(exec_request)
        assert result.quality_gate["threshold"] == 0.6
        assert result.quality_gate["passed"] is False
        assert result.artifact_ids == []

    def test_assistant_content_persisted_on_hold(self, fsm, exec_request, monkeypatch):
        self._patch_confidence(monkeypatch, 0.2)
        fsm._run_finalize(exec_request)
        assert "Quality gate" in fsm.execution.assistant_content
        fsm.db.commit.assert_called()

    def test_selected_skill_validation_is_stored_and_can_lower_confidence(self, fsm, exec_request, monkeypatch):
        self._patch_confidence(monkeypatch, 0.9)
        fsm.execution.task_spec = {
            "user_signal": "export_docx",
            "artifact_intents": ["docx"],
            "selected_skill_name": "weekly-sales-report",
        }
        fsm.execution.observations = [
            SimpleNamespace(
                observation_type="skill_call",
                tool_name="weekly-sales-report",
                request_args={"name": "weekly-sales-report"},
                result_data={"body": "# Weekly Sales Report\n\n## KPI Section\n## Recommendations"},
                success=True,
                artifact_ids=None,
            ),
            SimpleNamespace(
                observation_type="synthesize",
                tool_name="synthesizer",
                request_args={},
                result_data={"report_card_payload": {"title": "Weekly Sales", "summary": "Only summary"}},
                success=True,
                artifact_ids=["art-1"],
            ),
        ]
        monkeypatch.setattr(
            "app.services.synexia.capability_router._select_finalize_report_card_payload",
            lambda observations, artifact_payload: {"title": "Weekly Sales", "summary": "Only summary"},
        )

        result = fsm._run_finalize(exec_request)
        assert result.confidence_factors["selected_skill_validation"]["is_ok"] is False
        assert "Selected skill validation" in result.assistant_content
        assert result.confidence <= 0.55


# ── 3. grade_quality_gate ────────────────────────────────────────────────

class TestGradeQualityGate:
    def test_absent_gate_counts_as_pass(self):
        g = grade_quality_gate({"assistant_content": "hi"}, True)
        assert g.passed is True

    def test_absent_gate_fails_when_hold_expected(self):
        g = grade_quality_gate({"assistant_content": "hi"}, False)
        assert g.passed is False

    def test_passed_gate_matches_expectation(self):
        out = {"quality_gate": {"passed": True}}
        assert grade_quality_gate(out, True).passed is True
        assert grade_quality_gate(out, False).passed is False

    def test_held_gate_matches_expectation(self):
        out = {"quality_gate": {"passed": False, "held_artifact_ids": ["a"]}}
        assert grade_quality_gate(out, False).passed is True
        assert grade_quality_gate(out, True).passed is False

    def test_scenario_runner_integration(self):
        """The EvalRunner wires expect.quality_gate_passed to the grader."""
        import asyncio
        from app.services.synexia.eval_harness import EvalRunner

        async def run_fn(scenario):
            return {"quality_gate": {"passed": True}, "confidence": 0.9}

        runner = EvalRunner(run_fn=run_fn, scenarios=[{
            "name": "gate_ok",
            "user_message": "x",
            "expect": {"quality_gate_passed": True},
        }])
        report = asyncio.run(runner.run())
        assert report.is_ok
        assert report.scenarios[0].graders[0].name == "quality_gate_passed(True)"
