"""Tests for the workflow_builder system prompt (Task 2).

The workflow_builder agent designs AI-agent workflow graphs from plain-English
requests and must emit a valid `:::decision-summary` block. Its system prompt
must carry the node-type registry so emitted nodes only use known types.
"""

from app.services.agent_prompts import get_system_prompt


def test_workflow_builder_prompt_allows_large_graphs():
    p = get_system_prompt("workflow_builder")
    assert "3 to 20 nodes" in p or "3-20 nodes" in p
    assert "include ALL steps" in p or "do not simplify away" in p


def test_workflow_builder_prompt_forbids_tools():
    p = get_system_prompt("workflow_builder")
    assert "no tools" in p.lower()
    assert "never attempt tool calls" in p.lower() or "do not call" in p.lower()


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


def test_workflow_builder_prompt_warns_about_unconnected_switch_ports():
    """AI-built switches must not leave the default port unconnected — an
    unconnected port silently kills the branch (empty reply in chat)."""
    p = get_system_prompt("workflow_builder")
    assert "source-main-3" in p
    assert "fallback_label" in p
    assert "unconnected port" in p
    assert "{{inputs.<orchestrator>.text}}" in p


def test_workflow_builder_prompt_teaches_merge_convergence():
    """Rule 10: the builder knows merge appends ALL connected inputs and that
    the second input handle is target-main-1."""
    p = get_system_prompt("workflow_builder")
    assert "MERGE" in p
    assert "appends every item from ALL connected inputs" in p
    assert "target-main-1" in p


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


def test_workflow_builder_is_exempt_from_delegation_directive():
    """A semicolon-list workflow spec must NOT trigger the delegate_task nudge.

    2026-09-02 regression: ``delegation_nudge_directive`` counts ASCII ``;`` as a
    Chinese enumeration separator, so a workflow spec that lists node types
    ("a chat trigger node; an LLM node that classifies...; a switch node...;
    ... Connect everything in order") scored 8 "independent clauses" and the
    v3 stream injected "PARALLEL WORK DETECTED: ... Call delegate_task(tasks=
    [...])" into the builder's system prompt — plus the hard enforcement at
    iter=2 (observed in backend logs). The builder has NO delegate_task tool,
    so the model wasted iterations arguing with the guardrail instead of
    emitting the save_flow decision summary.
    """
    from app.services.delegation_nudge import delegation_nudge_directive

    prompt = (
        "Create a workflow called 'Sales Assistant' that answers sales "
        "questions. It should have: a chat trigger node; an LLM node that "
        "classifies the question into exactly one of SALES, CUSTOMERS, or "
        "GENERAL; a switch node that routes on that classification; a "
        "database query node in natural language mode for the SALES branch; "
        "a database query node for the CUSTOMERS branch; an agent node using "
        "general_assistant for the GENERAL branch; an LLM node that merges "
        "the branch answer into a final response; and a markdown output "
        "node. Connect everything in order."
    )
    # Sanity: the heuristic still fires on the raw prompt...
    assert delegation_nudge_directive(prompt) is not None
    # ...but the builder is exempt (its deliverable is the graph, and it has
    # no delegate_task tool to act on the directive).
    assert delegation_nudge_directive(prompt, agent_name="workflow_builder") is None
    # Other agents keep the old behavior.
    assert delegation_nudge_directive(prompt, agent_name="general_assistant") is not None


def test_builder_registry_block_embeds_field_names_and_option_values():
    """The registry block must expose config field names AND option VALUES.

    2026-09-02 regression: the block only listed REQUIRED fields
    ("db_query | Database Query | required config: kb_id"). The LLM had no
    way to know db_query's mode option values are ``sql|nl`` (not the
    display label "natural_language") or that the NL question field is named
    ``question`` — it invented both, and the saved node executed an empty
    SQL statement (MySQL error 1065 "Query was empty" on node n4).
    """
    from app.services.agent_prompts import _workflow_builder_registry_block

    block = _workflow_builder_registry_block()
    assert "question" in block  # the NL question field name
    assert "sql|nl" in block or "sql / nl" in block  # mode option VALUES
    assert "natural_language" not in block  # never advertise the wrong value
    # The db_query entry must carry its full config shape, not just kb_id.
    db_entry = next((l for l in block.splitlines() if l.startswith("- db_query")), "")
    assert "kb_id" in db_entry
    assert "mode" in db_entry

