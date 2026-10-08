"""Regression tests: the default plan must NOT fabricate a placeholder tool node.

Observed live 2026-10-08 (general_assistant chat): the deterministic default
plan appended a tool node literally named "Process request" whenever the
builder produced nothing. _execute_tool_node dispatched that name via
execute_tool → "Unknown tool: Process request", the corrective re-plan
retried and failed identically, and FINALIZE narrated the plumbing failure
to the user as "no data source is connected". The fix:

1. plan_dag._build_default_plan returns an EMPTY list instead of the
   placeholder node (execute_plan_nodes and every plan.nodes consumer
   handle zero nodes).
2. fsm._build_response_prompt treats a zero-NODE plan as a deliberate
   no-tool turn: direct-answer instructions, no "explain what went wrong"
   failure framing.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.synexia.fsm import ExecutionRequest, SynexiaFSM
from app.services.synexia.plan_dag import _build_default_plan


class TestDefaultPlanHasNoPlaceholderTool:
    def test_general_task_yields_empty_plan(self):
        """task_kind=general, no data, no artifacts → zero nodes, and the
        placeholder name must never appear."""
        steps = _build_default_plan({"task_kind": "general"}, "general_assistant")
        assert steps == []
        assert "Process request" not in str(steps)

    def test_empty_task_spec_yields_empty_plan(self):
        steps = _build_default_plan({}, "general_assistant")
        assert steps == []

    def test_data_task_still_emits_nl2sql(self):
        """The empty-plan change must not disable the data path."""
        steps = _build_default_plan(
            {"task_kind": "answer_question", "requires_data": True},
            "general_assistant",
        )
        assert steps, "requires_data plan must still contain nodes"
        assert any(s.get("node_type") == "nl2sql" for s in steps)

    def test_artifact_task_still_emits_sandbox(self):
        steps = _build_default_plan(
            {"task_kind": "general", "artifact_intents": ["docx"]},
            "general_assistant",
        )
        assert any(s.get("node_type") == "sandbox" for s in steps)


class TestExecutePlanNodesEmptyPlan:
    def test_zero_node_plan_executes_nothing(self):
        from app.services.synexia.capability_router import execute_plan_nodes

        plan = SimpleNamespace(nodes=[])
        observations = execute_plan_nodes(
            db=SimpleNamespace(),  # must not be touched
            execution=SimpleNamespace(),
            plan=plan,
        )
        assert observations == []


@pytest.fixture()
def fsm_no_tool_turn():
    """FSM whose plan completed with ZERO nodes and zero observations —
    the exact shape produced by the empty default plan."""
    f = SynexiaFSM.__new__(SynexiaFSM)
    f.db = None
    f.plan = SimpleNamespace(nodes=[])
    f.execution = SimpleNamespace(
        id="exec-test",
        observations=[],
        task_spec={},
        context_manifest=None,
    )
    return f


@pytest.fixture()
def fsm_failed_nodes():
    """FSM whose plan HAD nodes that failed before producing observations —
    must keep the old failure-framing behavior."""
    f = SynexiaFSM.__new__(SynexiaFSM)
    f.db = None
    f.plan = SimpleNamespace(nodes=[SimpleNamespace(name="Retrieve data")])
    f.execution = SimpleNamespace(
        id="exec-test",
        observations=[],
        task_spec={},
        context_manifest=None,
    )
    return f


def _request():
    return ExecutionRequest(
        conversation_id="conv-test",
        agent_name="general_assistant",
        user_message="tell me about 示例工厂 sales",
    )


class TestResponsePromptZeroNodePlan:
    def test_zero_node_plan_gets_direct_answer_note(self, fsm_no_tool_turn):
        prompt = fsm_no_tool_turn._build_response_prompt(_request())
        assert "No tools or data queries were scheduled for this turn" in prompt
        assert "NOT a failure" in prompt
        # No failure framing for a turn that never scheduled anything.
        assert "If there were errors, explain what went wrong" not in prompt
        # Anti-fabrication guard: must not claim a disconnected data source.
        assert "do NOT claim a data source is disconnected" in prompt

    def test_failed_nodes_keep_error_framing(self, fsm_failed_nodes):
        prompt = fsm_failed_nodes._build_response_prompt(_request())
        assert "If there were errors, explain what went wrong" in prompt
        assert "No tools or data queries were scheduled for this turn" not in prompt

    def test_plan_with_nodes_and_no_observations_unchanged(self, fsm_failed_nodes):
        """Zero observations but a real plan → findings text still shows the
        'No actions taken.' sentinel (pre-existing behavior)."""
        prompt = fsm_failed_nodes._build_response_prompt(_request())
        assert "No actions taken" in prompt
