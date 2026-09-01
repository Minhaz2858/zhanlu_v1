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
    # "trigger_chat" alone is not proof the registry is populated — RULE 3
    # mentions it regardless. Assert the registry block is present, is not the
    # degraded fallback, and carries at least one real registry entry line.
    assert "NODE-TYPE REGISTRY" in p
    assert "(registry unavailable)" not in p
    assert "- llm |" in p or "- db_query |" in p


def test_workflow_builder_prompt_is_not_generic_fallback():
    """The workflow_builder must NOT fall back to the generic assistant prompt."""
    p = get_system_prompt("workflow_builder")
    # "workflow" alone is tautological (the generic prompt contains it too).
    # Use a workflow_builder-unique marker plus a direct comparison against
    # the generic fallback.
    assert "NODE-TYPE REGISTRY" in p
    assert p != get_system_prompt(None)


def test_workflow_builder_prompt_contains_save_flow_action():
    p = get_system_prompt("workflow_builder")
    assert "save_flow" in p


def test_workflow_builder_prompt_mentions_node_shape():
    p = get_system_prompt("workflow_builder")
    assert '"type"' in p or "node_type" in p


def test_workflow_builder_prompt_requires_output_node():
    """validate_flow hard-requires an output node; the prompt must demand one."""
    p = get_system_prompt("workflow_builder")
    assert "at least one `output` node" in p


def test_workflow_builder_prompt_template_example_matches_runtime():
    """The runtime exposes {{inputs.<node>.text}} / .history, never chatInput."""
    p = get_system_prompt("workflow_builder")
    assert "{{inputs.n1.text}}" in p
    assert "{{inputs.n1.history}}" in p
    assert "chatInput" not in p


def test_workflow_builder_prompt_shows_edge_object_shape():
    """validate_flow requires edges with source/target — the prompt must show it."""
    p = get_system_prompt("workflow_builder")
    assert (
        '"source": "n1", "target": "n2", '
        '"sourceHandle": "source-main", "targetHandle": "target-main"' in p
    )
