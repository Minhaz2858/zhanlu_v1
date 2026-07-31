"""Regression: skill_agent must have the full zhanlu toolset (2026-07-28).

Before the spec change, skill_agent only had skill-management + web tools.
After: it has the same broader toolset as general_assistant, and the model
is bumped from gpt-4o-mini to gpt-4o.
"""

import pytest


def test_skill_agent_prompt_mentions_broader_capabilities():
    from app.services.agent_prompts import SKILL_AGENT_SYSTEM_PROMPT
    text = SKILL_AGENT_SYSTEM_PROMPT.lower()
    # The new paragraph in the prompt explicitly lists these capabilities.
    assert "memory" in text
    assert "code execution" in text or "code_execution" in text
    assert "file" in text  # file operations


def test_skill_agent_prompt_keeps_skill_creation_focus():
    from app.services.agent_prompts import SKILL_AGENT_SYSTEM_PROMPT
    text = SKILL_AGENT_SYSTEM_PROMPT.lower()
    assert "skill" in text
    assert "specializ" in text or "specialis" in text


def test_skill_agent_registry_model_is_gpt4o():
    """skill_agent must be on gpt-4o (not gpt-4o-mini) as of 2026-07-28."""
    from app.services.system_agents import _build_system_agent_configs

    # registry=None bypasses the live-registry filter so the assertion
    # checks the source-of-truth list, not the test env's empty registry.
    configs = _build_system_agent_configs(registry=None)
    skill = next((c for c in configs if c.get("name") == "skill_agent"), None)
    assert skill is not None, "skill_agent missing from system agent configs"
    assert skill.get("model") == "gpt-4o", (
        f"skill_agent.model should be gpt-4o, got {skill.get('model')!r}"
    )


def test_skill_agent_registry_has_broader_toolset():
    """skill_agent must include general_assistant tools (memory, code_execution, file ops, etc.)."""
    from app.services.system_agents import _build_system_agent_configs

    configs = _build_system_agent_configs(registry=None)
    skill = next((c for c in configs if c.get("name") == "skill_agent"), None)
    assert skill is not None
    enabled = set(
        (skill.get("tool_config") or {}).get("enabled_tools") or []
    )
    # Spot-check: the broader toolset must include these.
    for required in ("memory", "execute_code", "read_file", "write_file",
                     "delegate_task", "agent_browser", "image_generation"):
        assert required in enabled, (
            f"skill_agent missing required broader tool: {required}"
        )
    # And must still have the skill-creation essentials.
    for required in ("create_skill", "update_skill", "search_skills",
                     "Skill", "create_artifact"):
        assert required in enabled, (
            f"skill_agent lost a core skill-creation tool: {required}"
        )
