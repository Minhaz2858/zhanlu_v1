"""Verify the Skill Agent prompt does NOT gate creation on search_skills results.

User bug 2026-07-28: "Create a new skill ppt making skills" caused the agent
to search twice (returned 0) and then say "Got it — stopping the skill
creation. What would you like to do instead?" — never calling create_skill.

Root cause: SKILL_AGENT_SYSTEM_PROMPT in agent_prompts.py told the agent to
'ALWAYS call search_skills ... If one [similar skill] does, suggest reusing
or modifying it instead of creating a duplicate.' The agent over-interpreted
this and refused to proceed when search returned 0 results too.

These tests pin the contract: search is for context, creation is unconditional
when the user explicitly asks for it.
"""

import importlib

agent_prompts = importlib.import_module("app.services.agent_prompts")


def test_skill_agent_prompt_no_gating_phrase():
    """The old gating phrase 'instead of creating a duplicate' must be gone.

    That phrasing encouraged the agent to treat the search as a duplicate-check
    gate, which made it stop creation when search returned 0 results (rather
    than just the case where a clearly-similar skill was found).
    """
    prompt = agent_prompts.SKILL_AGENT_SYSTEM_PROMPT
    assert "instead of creating a duplicate" not in prompt, (
        "Old gating phrase 'instead of creating a duplicate' is still in "
        "SKILL_AGENT_SYSTEM_PROMPT — this is what made the agent stop creating "
        "when search_skills returned 0. Replace with informational wording."
    )


def test_skill_agent_prompt_search_described_as_informational():
    """The Skill Discovery section should describe search as contextual —
    something that informs the design, not something that gates creation."""
    prompt = agent_prompts.SKILL_AGENT_SYSTEM_PROMPT.lower()
    # Search is mentioned (still useful for context)
    assert "search" in prompt, "Skill Discovery section should still call search_skills"
    # The prompt must explicitly say NOT to gate creation on search results.
    assert (
        "do not gate" in prompt
        or "don't gate" in prompt
        or "unconditional" in prompt
    ), (
        "Skill Discovery section should explicitly state that search results "
        "do NOT gate creation. Use language like 'do not gate creation' or "
        "'unconditional'."
    )


def test_skill_agent_prompt_unconditional_creation_after_user_request():
    """When the user asks to create, the agent must create — regardless of search."""
    prompt = agent_prompts.SKILL_AGENT_SYSTEM_PROMPT.lower()
    # Must mention that if the user explicitly asked to create, we create it.
    assert (
        "if the user" in prompt and "create" in prompt and "explicitly" in prompt
    ) or (
        "user explicitly asked to create" in prompt
    ) or (
        "user asks to create" in prompt
    ), (
        "Skill Agent prompt should explicitly state that when the user asks "
        "to create a new skill, creation proceeds unconditionally."
    )


def test_skill_agent_tools_unaffected():
    """Tool definitions must still include create_skill + update_skill."""
    tool_names = {t["function"]["name"] for t in agent_prompts.SKILL_AGENT_TOOLS}
    assert "create_skill" in tool_names
    assert "update_skill" in tool_names
    assert "search_skills" in tool_names
