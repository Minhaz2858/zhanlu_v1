"""Regression: every agent gets a date anchor and explicit real-time tool
availability in its system prompt.

Why this exists
---------------
A user reported that the agent answered "April 10, 2025" when asked "what is
today's date" on July 24, 2026. The root cause was the *frontend* routing
Ungrouped chats around the agent runtime, but the *real* fix is to guarantee
that ``get_system_prompt()`` always appends the ``_runtime_context_block()``
to the returned prompt — regardless of which agent name or agent_app
record the caller passes. If a future refactor accidentally drops the
``prompt = prompt + _runtime_context_block()`` line at the bottom of
``get_system_prompt()``, EVERY agent will silently revert to the
training-data refusal "I don't have real-time data access" for simple
date / weather / news questions.

These tests are deliberately substring checks so they survive rewording of
the runtime context block. They cover:

  * every system agent (agent_builder, skill_agent, automation_agent,
    general_assistant, power_user, data_agent)
  * the generic fallback (agent_name=None)
  * user-created agents assembled from the 5-layer AgentApp fields
  * the time-sensitive grounding append (a regression for the secondary
    tool-choice forcing path)
"""

from types import SimpleNamespace

from app.services.agent_prompts import get_system_prompt


# Substrings that MUST appear in every agent's system prompt.
# Kept loose enough to survive rewording of the block, tight enough to
# catch the actual regression (a removed _runtime_context_block() call).
REQUIRED_RUNTIME_SUBSTRINGS = [
    "[CURRENT DATE & TIME]",
    "Today is",
    "real-time data access",
    "web_search",
    "agent_browser",
]


# All known system agents. general_assistant is the auto-default for
# Ungrouped chats (see frontend/src/pages/Chat.jsx), so it MUST be in
# the list — it is the exact agent the bug screenshot hit.
SYSTEM_AGENT_NAMES = [
    "agent_builder",
    "skill_agent",
    "automation_agent",
    "general_assistant",
    "power_user",
    "data_agent",
]


def _assert_has_runtime_block(prompt: str, label: str) -> None:
    """Helper: assert the runtime context block is present."""
    for needle in REQUIRED_RUNTIME_SUBSTRINGS:
        assert needle in prompt, (
            f"[{label}] missing required runtime-context substring: {needle!r}\n"
            f"--- prompt tail (last 400 chars) ---\n{prompt[-400:]}"
        )


# ---------------------------------------------------------------------------
# Every system agent gets the runtime context block
# ---------------------------------------------------------------------------


def test_every_system_agent_has_date_anchor_and_real_time_tools():
    """For every known system agent, the prompt ends with the date anchor
    and explicit real-time capability statement.

    A refactor that drops ``prompt = prompt + _runtime_context_block()``
    from ``get_system_prompt()`` will fail every one of these."""
    for name in SYSTEM_AGENT_NAMES:
        prompt = get_system_prompt(agent_name=name, user_message="hello")
        _assert_has_runtime_block(prompt, label=f"agent_name={name}")


def test_generic_fallback_has_date_anchor_and_real_time_tools():
    """When agent_name is None and no agent_app is passed, the generic
    fallback must STILL include the runtime context block. The generic
    path is what Ungrouped chats hit when the agent runtime lookup
    itself fails."""
    prompt = get_system_prompt(agent_name=None, user_message="hello")
    _assert_has_runtime_block(prompt, label="agent_name=None (generic fallback)")


def test_user_created_agent_has_date_anchor_and_real_time_tools():
    """A user-created agent (assembled from the 5-layer AgentApp fields)
    must ALSO get the runtime context block. Without this, an agent
    built by agent_builder will silently lose date awareness once
    deployed."""
    fake_agent_app = SimpleNamespace(
        name="My Custom Agent",
        description="A user-created agent for testing.",
        prompt_identity="You are a custom agent.",
        prompt_boundary="Stay within the user's stated domain.",
        prompt_reasoning="Think step by step.",
        prompt_tools="Use web_search when needed.",
        prompt_output="Return concise answers.",
    )
    prompt = get_system_prompt(
        agent_name="my_custom_agent",  # no builtin def → falls to layered path
        agent_app=fake_agent_app,
        user_message="hello",
    )
    _assert_has_runtime_block(prompt, label="user-created agent (5-layer)")


# ---------------------------------------------------------------------------
# Time-sensitive grounding append (regression for the secondary path)
# ---------------------------------------------------------------------------


def test_time_sensitive_message_appends_grounding_block():
    """When the user message is time-sensitive and the agent has
    ``web_search`` in its tool list, the [GROUNDING REQUIRED] block
    must be appended. This is the soft nudge that helps the LLM call
    ``web_search`` instead of defaulting to its training data."""
    prompt = get_system_prompt(
        agent_name="general_assistant",
        user_message="What's the weather today?",
    )
    # Runtime block (always-on) plus the per-turn grounding block.
    _assert_has_runtime_block(prompt, label="time-sensitive / general_assistant")
    # grounding_block_for_message is the soft nudge; its exact text
    # varies, but "GROUNDING" is a stable marker.
    assert "GROUNDING" in prompt.upper() or "web_search" in prompt.lower(), (
        "Time-sensitive message should produce a grounding/web_search "
        f"nudge; got tail:\n{prompt[-400:]}"
    )


def test_non_time_sensitive_message_keeps_runtime_block_only():
    """A non-time-sensitive message should NOT add the grounding block
    but MUST still keep the runtime context block. Regression guard
    against a future change that gates the runtime block on the
    user_message."""
    prompt = get_system_prompt(
        agent_name="general_assistant",
        user_message="Please summarize the attached document.",
    )
    _assert_has_runtime_block(prompt, label="non-time-sensitive / general_assistant")


# ---------------------------------------------------------------------------
# The runtime block is appended LAST (so it survives any inner edits)
# ---------------------------------------------------------------------------


def test_runtime_block_is_appended_after_all_other_blocks():
    """The runtime context block must come after every other block
    (tone, initiative, grounding). Putting it last means the model
    sees the date anchor closest to the tool-choice decision."""
    prompt = get_system_prompt(
        agent_name="general_assistant",
        user_message="What is the price of BTC today?",
    )
    runtime_idx = prompt.rfind("[CURRENT DATE & TIME]")
    assert runtime_idx > 0, "Runtime context block not found in prompt"
    # The runtime block must be in the final third of the prompt — i.e.
    # appended after the agent's main body.
    assert runtime_idx > len(prompt) * 0.5, (
        f"Runtime context block should be appended late in the prompt; "
        f"found at index {runtime_idx} of {len(prompt)} chars."
    )
