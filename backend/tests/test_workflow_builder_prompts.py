"""Tests for the workflow_builder system prompt (Task 2).

The workflow_builder agent designs AI-agent workflow graphs from plain-English
requests and must emit a valid `:::decision-summary` block. Its system prompt
must carry the node-type registry so emitted nodes only use known types.
"""

from app.services.agent_prompts import get_system_prompt


def test_workflow_builder_prompt_has_registry():
    p = get_system_prompt("workflow_builder")
    assert "decision-summary" in p
    assert "trigger_chat" in p


def test_workflow_builder_prompt_is_not_generic_fallback():
    """The workflow_builder must NOT fall back to the generic assistant prompt."""
    p = get_system_prompt("workflow_builder")
    assert "workflow" in p.lower()


def test_workflow_builder_prompt_contains_save_flow_action():
    p = get_system_prompt("workflow_builder")
    assert "save_flow" in p


def test_workflow_builder_prompt_mentions_node_shape():
    p = get_system_prompt("workflow_builder")
    assert '"type"' in p or "node_type" in p
