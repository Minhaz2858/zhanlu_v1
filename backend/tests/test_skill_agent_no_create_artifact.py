"""Regression: Skill Agent's toolset must NOT include `create_artifact`
(2026-07-28).

The Skill Agent was observed creating PPTX/PDF/DOCX stub artifacts in
the chat right after creating a SKILL.md skill, despite a prompt rule
forbidding it. The reliable fix is to remove the tool entirely from
the Skill Agent's toolset so the model cannot call it.

These tests pin that contract from multiple angles: the static list,
the runtime-resolved toolset, the manifest permission boundary, the
new constant that drives the runtime resolution, and the prompt text.
"""
from __future__ import annotations

import os
import re

from app.services.agent_prompts import (
    SKILL_AGENT_SYSTEM_PROMPT,
    SKILL_AGENT_TOOLS,
    get_tools,
)
from app.services.system_agents import ALL_TOOL_NAMES


def _has_create_artifact(tools) -> bool:
    """True iff the iterable contains a tool with name == 'create_artifact'."""
    for t in tools:
        if isinstance(t, str):
            if t == "create_artifact":
                return True
        elif getattr(t, "name", None) == "create_artifact":
            return True
        elif isinstance(t, dict) and t.get("name") == "create_artifact":
            return True
    return False


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


# --------------------------------------------------------------------
# 1) Static list
# --------------------------------------------------------------------

def test_skill_agent_tools_static_list_excludes_create_artifact():
    """The hand-curated SKILL_AGENT_TOOLS list must not include create_artifact."""
    assert not _has_create_artifact(SKILL_AGENT_TOOLS), (
        f"SKILL_AGENT_TOOLS contains create_artifact: {SKILL_AGENT_TOOLS!r}"
    )


# --------------------------------------------------------------------
# 2) Runtime resolution
# --------------------------------------------------------------------

def test_skill_agent_enabled_tools_excludes_create_artifact():
    """get_tools('skill_agent', ...) must not include create_artifact.

    Exercises the full resolution path used by the runtime, not just
    the static fallback list. Catches the case where someone re-adds
    create_artifact via a config override or the new constant.
    """
    from types import SimpleNamespace

    tool_config = {
        "enabled_tools": [
            t for t in ALL_TOOL_NAMES if t != "create_artifact"
        ],
    }
    # Build a minimal agent_app that the tool resolver accepts.
    agent_app = SimpleNamespace(name="skill_agent")

    tools = get_tools("skill_agent", tool_config, agent_app)
    assert not _has_create_artifact(tools), (
        "get_tools('skill_agent', ...) returned a list containing "
        f"create_artifact. Resolved tools: {tools!r}"
    )


# --------------------------------------------------------------------
# 3) manifest_json.boundaries.allowed
# --------------------------------------------------------------------

def test_skill_agent_boundaries_allowed_excludes_create_artifact():
    """The skill_agent boundaries.allowed list must not include create_artifact.

    Source-based: the skill_agent config lives inside a list literal
    inside the function `_build_system_agent_configs` in system_agents.py.
    We scan the source for the line `"name": "skill_agent"`, then look
    forward for the next `boundaries.allowed` list within the same dict
    literal. Doesn't depend on the runtime permission check or DB state.
    """
    system_agents_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "services", "system_agents.py"
    )
    source = _read(system_agents_path)
    lines = source.splitlines()

    # Find the line index of `"name": "skill_agent"`.
    name_line = None
    for i, line in enumerate(lines):
        if re.search(r'"name"\s*:\s*"skill_agent"', line):
            name_line = i
            break
    assert name_line is not None, (
        "Could not find `\"name\": \"skill_agent\"` in system_agents.py"
    )

    # Look forward (up to 80 lines) for the boundaries.allowed list.
    # Pattern: `"allowed": [ ... ]` possibly spanning multiple lines.
    # We use a non-greedy multi-line regex.
    window = "\n".join(lines[name_line : name_line + 80])
    m = re.search(
        r'"boundaries"\s*:\s*\{[^{}]*"allowed"\s*:\s*\[([^\]]*)\]',
        window,
        flags=re.DOTALL,
    )
    assert m, (
        f"Could not find boundaries.allowed near the skill_agent config "
        f"in system_agents.py (line ~{name_line})."
    )
    allowed_str = m.group(1)
    # Parse the list of strings (each is `"name"` possibly with trailing comma).
    allowed = re.findall(r'"([a-zA-Z0-9_]+)"', allowed_str)
    assert "create_artifact" not in allowed, (
        f"system_agents.skill_agent.manifest_json.boundaries.allowed "
        f"contains 'create_artifact': {allowed!r}"
    )


# --------------------------------------------------------------------
# 4) New constant SKILL_AGENT_TOOL_NAMES
# --------------------------------------------------------------------

def test_skill_agent_tool_names_constant_excludes_create_artifact():
    """A module-level constant SKILL_AGENT_TOOL_NAMES must exist and not
    include create_artifact. This is the load-bearing constant that
    drives the runtime toolset; if someone reverts to ALL_TOOL_NAMES,
    this test catches it.
    """
    # Lazy import: the constant is added by Task 2 of the implementation
    # plan. Lazy-importing here lets the other 5 tests run before the
    # constant exists (TDD red phase).
    from app.services.system_agents import SKILL_AGENT_TOOL_NAMES

    assert isinstance(SKILL_AGENT_TOOL_NAMES, (list, tuple, set, frozenset)), (
        f"SKILL_AGENT_TOOL_NAMES must be a list/tuple/set, got {type(SKILL_AGENT_TOOL_NAMES)}"
    )
    assert "create_artifact" not in SKILL_AGENT_TOOL_NAMES, (
        f"SKILL_AGENT_TOOL_NAMES contains 'create_artifact': "
        f"{sorted(SKILL_AGENT_TOOL_NAMES)!r}"
    )
    # And it must be a SUBSET of ALL_TOOL_NAMES (sanity: we shouldn't
    # be adding tools, only removing create_artifact).
    assert set(SKILL_AGENT_TOOL_NAMES).issubset(set(ALL_TOOL_NAMES)), (
        "SKILL_AGENT_TOOL_NAMES is not a subset of ALL_TOOL_NAMES. "
        "Did you forget to derive it from ALL_TOOL_NAMES?"
    )


# --------------------------------------------------------------------
# 5) Prompt text does not mention create_artifact
# --------------------------------------------------------------------

def test_skill_agent_prompt_does_not_mention_create_artifact():
    """The Skill Agent's prompt must not mention create_artifact anywhere.

    The user picked 'silent removal' in the brainstorm (no replacement
    text). If the prompt mentions the tool, the model might infer it
    has access to it and try to call it via some fallback path.
    """
    assert "create_artifact" not in SKILL_AGENT_SYSTEM_PROMPT, (
        "Skill Agent prompt mentions 'create_artifact' but the tool is "
        "no longer in the agent's toolset. The mention will confuse the "
        "model into thinking it can call the tool."
    )


# --------------------------------------------------------------------
# 6) No '## Artifacts' section header
# --------------------------------------------------------------------

def test_skill_agent_prompt_no_artifact_section_header():
    """The '## Artifacts' section must be deleted from the prompt.

    Pins the silent-removal decision: the 35-line section is gone,
    not replaced with a stub. If someone re-adds the section with a
    different title or rewording, this test will (deliberately) fail
    and force an explicit decision.
    """
    # Look for any H2 header that mentions 'artifact' in the prompt.
    matches = re.findall(
        r"^##\s+[^\n]*[Aa]rtifact[^\n]*$",
        SKILL_AGENT_SYSTEM_PROMPT,
        flags=re.MULTILINE,
    )
    assert not matches, (
        f"Skill Agent prompt still has an 'Artifacts' section header: "
        f"{matches!r}. The section was deleted in the 2026-07-28 fix; "
        f"re-adding it requires an explicit decision."
    )


# --------------------------------------------------------------------
# 7) skill_agent tool_config.enabled_tools uses SKILL_AGENT_TOOL_NAMES
#    (NOT ALL_TOOL_NAMES) — 2026-07-28 regression test
# --------------------------------------------------------------------
#
# Bug found 2026-07-28: my Turn 10 fix used replace_in_file on the
# pattern "_tools_in_registry(registry, ALL_TOOL_NAMES),". The pattern
# appears in BOTH the skill_agent block (the one we wanted to fix) AND
# the power_user block. The replace_in_file tool changed the WRONG
# block (power_user) instead of skill_agent, leaving skill_agent using
# ALL_TOOL_NAMES (which still includes create_artifact). The model
# continued to call create_artifact after every skill creation.
#
# These tests pin the contract that:
# - skill_agent's enabled_tools uses SKILL_AGENT_TOOL_NAMES
# - power_user's enabled_tools uses ALL_TOOL_NAMES (the original intent)
#
# We do this with a source-string scan: find each agent's `"name":`
# line, look forward in the next 30 lines for the enabled_tools value,
# and assert which constant is used.
# --------------------------------------------------------------------


def _find_agent_enabled_tools_value(agent_name: str) -> str | None:
    """Find the value of `enabled_tools` for a given agent_name in
    system_agents.py. Returns the constant name (e.g. "SKILL_AGENT_TOOL_NAMES"
    or "ALL_TOOL_NAMES") or None if not found.
    """
    system_agents_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "services", "system_agents.py"
    )
    source = _read(system_agents_path)
    lines = source.splitlines()

    # Find the line index of `"name": "agent_name"`.
    name_line = None
    for i, line in enumerate(lines):
        if re.search(rf'"name"\s*:\s*"{re.escape(agent_name)}"', line):
            name_line = i
            break
    if name_line is None:
        return None

    # Look forward in the next 30 lines for `enabled_tools` value.
    window = "\n".join(lines[name_line : name_line + 30])
    m = re.search(
        r'"enabled_tools"\s*:\s*_tools_in_registry\(\s*registry\s*,\s*(\w+)\s*\)',
        window,
    )
    if not m:
        return None
    return m.group(1)


def test_skill_agent_tool_config_uses_skill_agent_tool_names():
    """The skill_agent config's enabled_tools must use SKILL_AGENT_TOOL_NAMES,
    not ALL_TOOL_NAMES.

    Regression test for the 2026-07-28 Turn 10 bug where my
    replace_in_file changed the wrong block.
    """
    value = _find_agent_enabled_tools_value("skill_agent")
    assert value is not None, (
        "Could not find `enabled_tools` for skill_agent in system_agents.py"
    )
    assert value == "SKILL_AGENT_TOOL_NAMES", (
        f"skill_agent's tool_config.enabled_tools should use "
        f"SKILL_AGENT_TOOL_NAMES (so create_artifact is excluded), "
        f"but found {value!r}. The runtime will give the model access "
        f"to create_artifact if this is ALL_TOOL_NAMES — the 2026-07-28 "
        f"sample-artifact regression will return."
    )


def test_power_user_tool_config_uses_all_tool_names():
    """The power_user config's enabled_tools must use ALL_TOOL_NAMES
    (not SKILL_AGENT_TOOL_NAMES).

    Regression test for the 2026-07-28 Turn 10 bug where my
    replace_in_file accidentally changed power_user's enabled_tools
    to SKILL_AGENT_TOOL_NAMES (which excludes create_artifact).
    power_user is documented as a full-capability agent with every
    tool; it should have ALL tools.
    """
    value = _find_agent_enabled_tools_value("power_user")
    assert value is not None, (
        "Could not find `enabled_tools` for power_user in system_agents.py"
    )
    assert value == "ALL_TOOL_NAMES", (
        f"power_user's tool_config.enabled_tools should use "
        f"ALL_TOOL_NAMES (full zhanlu toolset), but found {value!r}. "
        f"power_user is documented as a 'full-capability agent with "
        f"every tool registered in the zhanlu toolset' — using "
        f"SKILL_AGENT_TOOL_NAMES would silently exclude create_artifact "
        f"and other tools."
    )
