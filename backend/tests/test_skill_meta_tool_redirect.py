"""Tests for the Skill meta-tool tool-name redirect.

The LLM sometimes conflates the ``Skill`` dispatcher with the underlying
tools and calls e.g. ``Skill {"command": "load_skill_body"}`` — passing a
*tool* name as the *skill* name. The meta-tool must detect this and return a
self-correcting redirect instead of a dead-end "Skill not found" error.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))


@pytest.mark.parametrize(
    "command",
    [
        "load_skill_body",
        "execute load_skill_body",
        "skills",
        "list_default_skills",
        "ask_data_agent",
        "ask_perception_intelligence_diagnosis",
    ],
)
def test_skill_meta_tool_redirects_tool_names(command):
    """A tool name passed as a Skill command returns a redirect, not 'not found'."""
    from app.services.skill_routing.meta_tool import _skill_meta_tool

    result = asyncio.run(_skill_meta_tool({"command": command}))
    assert result["success"] is False
    assert "tool name" in result["error"]
    assert "execute <skill-name>" in result["error"]
    assert "Skill not found" not in result["error"]


def test_skill_meta_tool_redirect_mentions_direct_invocation():
    """The redirect must tell the LLM to call the tool directly instead."""
    from app.services.skill_routing.meta_tool import _skill_meta_tool

    result = asyncio.run(_skill_meta_tool({"command": "load_skill_body"}))
    assert "invoke it directly" in result["error"]


def test_real_skill_names_not_in_redirect_set():
    """Real skill names (e.g. weekly-report-generation) must not be redirected."""
    from app.services.skill_routing.meta_tool import _TOOL_NAMES_MISTAKEN_AS_SKILLS

    assert "weekly-report-generation" not in _TOOL_NAMES_MISTAKEN_AS_SKILLS
    assert "pptx" not in _TOOL_NAMES_MISTAKEN_AS_SKILLS
    assert "docx" not in _TOOL_NAMES_MISTAKEN_AS_SKILLS


def test_parse_action_unchanged():
    """_parse_action still splits action prefixes correctly."""
    from app.services.skill_routing.meta_tool import _parse_action

    assert _parse_action("execute weekly-report-generation") == (
        "execute",
        "weekly-report-generation",
    )
    assert _parse_action("load pptx") == ("load", "pptx")
    assert _parse_action("pptx") == ("load", "pptx")


@pytest.mark.parametrize("command", ["execute", "load"])
def test_bare_action_prefix_returns_instructive_error(command):
    """A bare action prefix (no skill name) must return an instructive error
    telling the LLM to include the skill name — NOT the dead-end
    'Skill not found: execute' seen in production."""
    from app.services.skill_routing.meta_tool import _skill_meta_tool

    result = asyncio.run(_skill_meta_tool({"command": command}))
    assert result["success"] is False
    assert "action prefix" in result["error"]
    assert "execute <skill-name>" in result["error"]
    assert "Skill not found" not in result["error"]
