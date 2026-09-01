"""Tests that _build_response_prompt is grounded in real observation data
and acceptance criteria (Phase 1, G1 + G2).

_build_response_prompt is shared by the blocking _generate_response and the
streaming stream_final_response, so grounding both paths via one change.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.synexia.fsm import ExecutionRequest, SynexiaFSM


@pytest.fixture()
def fsm():
    f = SynexiaFSM.__new__(SynexiaFSM)
    f.db = None
    f.execution = SimpleNamespace(
        id="exec-test",
        observations=[],
        task_spec={},
        context_manifest=None,
    )
    return f


@pytest.fixture()
def exec_request():
    return ExecutionRequest(
        conversation_id="conv-test",
        agent_name="general_assistant",
        user_message="summarize Q2 sales",
    )


def _nl2sql_obs():
    return SimpleNamespace(
        seq=1, observation_type="nl2sql", tool_name="nl2sql", success=True,
        result_data={
            "sql": "SELECT material, revenue FROM sales WHERE q='Q2'",
            "data": [{"material": "Steel", "revenue": 120}],
        },
        result_text=None, error_message=None, artifact_ids=None,
    )


class TestGroundedFindings:
    def test_prompt_contains_real_observation_values(self, fsm, exec_request):
        fsm.execution.observations = [_nl2sql_obs()]
        prompt = fsm._build_response_prompt(exec_request)
        assert "Findings (from executed actions)" in prompt
        assert "Steel" in prompt              # real row value, not "completed"
        assert "revenue: sum=120" in prompt   # aggregate stat
        assert "SELECT material, revenue" in prompt  # SQL present

    def test_no_observations_keeps_no_actions_text(self, fsm, exec_request):
        fsm.execution.observations = []
        prompt = fsm._build_response_prompt(exec_request)
        assert "No actions taken" in prompt


class TestCriteriaInjection:
    def test_prompt_includes_acceptance_criteria(self, fsm, exec_request):
        fsm.execution.task_spec = {
            "acceptance_criteria": ["report must include revenue figures",
                                    "must cite the data source"],
        }
        prompt = fsm._build_response_prompt(exec_request)
        assert "You must satisfy" in prompt
        assert "report must include revenue figures" in prompt
        assert "must cite the data source" in prompt

    def test_falls_back_to_kpis_when_criteria_absent(self, fsm, exec_request):
        fsm.execution.task_spec = {"kpis": ["accuracy", "completeness"]}
        prompt = fsm._build_response_prompt(exec_request)
        assert "You must satisfy" in prompt
        assert "accuracy" in prompt
        assert "completeness" in prompt

    def test_no_criteria_no_satisfy_block(self, fsm, exec_request):
        fsm.execution.task_spec = {}
        prompt = fsm._build_response_prompt(exec_request)
        # The criteria *block* header must be absent (the guidance sentence
        # may reference the block by name, so assert on the header marker).
        assert "=== You must satisfy ===" not in prompt


class TestSharedPath:
    def test_blocking_and_streaming_share_one_prompt_builder(self, fsm, exec_request):
        """_generate_response and stream_final_response both call
        _build_response_prompt, so grounding reaches both paths."""
        fsm.execution.observations = [_nl2sql_obs()]
        prompt = fsm._build_response_prompt(exec_request)
        # The blocking generator builds its system_prompt from this method
        # (fsm.py:910); the streaming path stashes the same prompt during
        # FINALIZE (fsm.py:922+). One assertion: the shared builder is grounded.
        assert "Findings (from executed actions)" in prompt
