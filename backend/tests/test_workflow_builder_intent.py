"""Intent-routing tests for the workflow_creation header regex.

The workflow-creation intent sits between automation_setup and
agent_creation in ``_SYSTEM_AGENT_INTENT_TABLE`` (most-specific first).
These tests pin:

- WORKFLOW NOUN: EN/ZH verbs (create/build/make/design/set up/new,
  创建/新建/做/构建/设计) route to (workflow_creation, workflow_builder),
  including "agent workflow" / "智能体工作流" compound nouns.
- COLLISION LOOKAHEADS: "workflow agent" / "workflow skill" (and the ZH
  equivalents 工作流智能体 / 工作流技能) are an AGENT / SKILL, not a
  workflow — the negative lookahead in the workflow header must reject
  them so they fall through to agent_creation / skill_creation.
- ZH agent/skill headers still catch their own nouns after the
  workflow middle-token extension (the 工作流 token).
"""

from __future__ import annotations

import os

# Override UPLOAD_DIR before any app.* import (mirrors sibling file).
os.environ.setdefault("UPLOAD_DIR", "/tmp/test_uploads_workflow_builder_intent")
os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)

import pytest

from app.routers.agents import _detect_system_agent_intent


def test_workflow_creation_intent_en():
    assert _detect_system_agent_intent("create a workflow that checks open orders") == ("workflow_creation", "workflow_builder")


def test_workflow_creation_intent_zh():
    assert _detect_system_agent_intent("帮我做一个工作流，每天汇总销售") == ("workflow_creation", "workflow_builder")


def test_agent_creation_still_routes_to_agent_builder():
    assert _detect_system_agent_intent("create a new agent for support") == ("agent_creation", "agent_builder")


@pytest.mark.parametrize(
    "message",
    [
        # EN verbs — noun is "workflow" (or "agent workflow").
        "create an agent workflow",
        "build me a workflow for invoicing",
        "make a workflow for weekly reports",
        "design a workflow for approvals",
        "set up a workflow for invoicing",
        "new workflow please",
        # ZH verbs.
        "创建一个工作流",
        "新建一个工作流",
        "构建一个工作流",
        "设计一个工作流",
        "帮我设计一个工作流",
        "创建一个智能体工作流",
    ],
)
def test_workflow_creation_detected(message: str) -> None:
    """Workflow-noun headers must route to workflow_builder."""
    assert _detect_system_agent_intent(message) == ("workflow_creation", "workflow_builder"), message


def test_workflow_agent_collision_routes_to_agent_builder() -> None:
    """\"workflow agent\" is an AGENT — the lookahead rejects the workflow
    prefix and the agent header (workflow middle token) catches it."""
    for message in (
        "create a workflow agent",
        "build me a workflow agent",
        "create a workflow agent builder",
        "set up a workflow agent",
    ):
        assert _detect_system_agent_intent(message) == ("agent_creation", "agent_builder"), message


def test_workflow_skill_collision_routes_to_skill_agent() -> None:
    """\"workflow skill\" is a SKILL — falls through to skill_creation."""
    assert _detect_system_agent_intent("create a workflow skill") == ("skill_creation", "skill_agent")


def test_zh_workflow_agent_collision_routes_to_agent_builder() -> None:
    """ZH mirror: 工作流智能体 must route to agent_creation, not the
    workflow prefix."""
    assert _detect_system_agent_intent("创建一个工作流智能体") == ("agent_creation", "agent_builder")


def test_zh_agent_creation_still_routes_to_agent_builder() -> None:
    """The ZH agent header still catches its own noun after the
    工作流 middle-token extension."""
    assert _detect_system_agent_intent("创建一个智能体") == ("agent_creation", "agent_builder")
