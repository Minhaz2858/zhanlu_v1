"""Regression tests for the generic system-agent routing helper.

Builds on ``test_automation_setup_intent.py`` (which locked in the
automation-setup intent). Adds the two sibling intents that share the
same chat-router fix:

* **agent_creation** → rebind to ``agent_builder``
* **skill_creation** → rebind to ``skill_agent``

User reports driving this: a chat bound to a project whose auto-selected
agent (e.g. ``general_assistant``) lacks the dedicated system agent's
toolset would misfire on ``ask_data_agent`` / general tools. Generic
route-on-intent keeps each system agent's purpose intact (no toolset
pollution) while still sending the user to the right tool on paste.

These tests pin:
- INTENT DETECTION: each header regex + structure regex (where one
  exists) returns the right ``(intent_name, target_agent)`` tuple.
- PRIORITY ORDERING: when multiple intents could match (e.g.
  "create a new agent to schedule tasks"), the FIRST in the intent
  table wins; the test confirms intentional non-overlap behaviour.
- NEGATIVE COVERAGE: random data queries, generic "create" templates
  without a target noun, English-only skills that mention "create"
  incidentally do NOT trigger any intent.
- BACKWARD-COMPAT SHIMS: ``_detect_automation_setup_intent`` and
  ``_apply_automation_setup_routing`` still return the legacy
  ``bool`` shape so older callers/tests don't break.
"""

from __future__ import annotations

import os

# Override UPLOAD_DIR before any app.* import (mirrors sibling file).
os.environ.setdefault("UPLOAD_DIR", "/tmp/test_uploads_system_agent_intent")
os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)

import pytest

from app.routers.agents import (
    _detect_system_agent_intent,
    _detect_automation_setup_intent,  # backward-compat shim
    _apply_automation_setup_routing,  # backward-compat shim
    _AGENT_CREATION_HEADER_RE,
    _SKILL_CREATION_HEADER_RE,
    _SYSTEM_AGENT_INTENT_TABLE,
)


# ── intent detection: agent_creation ────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        "Create a new agent for me that summarizes sales reports",
        "Create an agent for me to handle customer onboarding",
        "Create me an AI agent",
        "create a custom agent for our support team",
        "build me an agent that monitors inventory",
        "Build an agent for Q2 reporting",
        "Make me an agent that integrates with our ERP",
        "Set up a new agent for data analysis",
        "Design an agent for sales coaching",
        "新建一个智能体",
        "新建一个AI智能体",
        "创建智能体",
        "创建一个自定义智能体",
        "帮我创建一个AI智能体",
        "做一个智能体",
        "构建智能体",
        "添加智能体",
    ],
)
def test_agent_creation_detected(message: str) -> None:
    """Headers that mean "build me an agent" must route to agent_builder."""
    detected = _detect_system_agent_intent(message)
    assert detected is not None, message
    intent, target = detected
    assert intent == "agent_creation", message
    assert target == "agent_builder", message


# ── intent detection: skill_creation ────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        "Create a new skill",
        "Create a custom skill for document summarization",
        "build me a skill that auto-generates test cases",
        "Make a reusable skill that translates Chinese",
        "Set up a custom skill for PDF extraction",
        "Design a reusable skill",
        "Write the SKILL.md for our methodology",
        "write a skill for code review",
        "add a skill that wraps the docker-compose skill",
        "新建一个技能",
        "新建一个可复用技能",
        "创建技能",
        "创建一个可复用技能",
        "做一个技能",
        "写一个技能",
        "添加一个技能",
    ],
)
def test_skill_creation_detected(message: str) -> None:
    """Headers that mean "build me a skill" must route to skill_agent."""
    detected = _detect_system_agent_intent(message)
    assert detected is not None, message
    intent, target = detected
    assert intent == "skill_creation", message
    assert target == "skill_agent", message


# ── intent detection: automation_setup still works (sanity) ─────────────


@pytest.mark.parametrize(
    "message",
    [
        "Create a new Automation Task:\n- Name: Daily Sync",
        "帮我新建一个自动化任务:\n- 名称: 销售数据",
        # Structural-only automation template
        "- Name: Foo\n- Schedule: daily\n- Output format: html",
    ],
)
def test_automation_setup_still_detected(message: str) -> None:
    """The first-deployed intent still routes to automation_agent."""
    detected = _detect_system_agent_intent(message)
    assert detected is not None, message
    intent, target = detected
    assert intent == "automation_setup"
    assert target == "automation_agent"


# ── priority ordering: most-specific wins ────────────────────────────────


def test_priority_automation_over_agent_creation() -> None:
    """A message that LOOKS like it could be either intent must resolve
    to automation_setup first because "automation task" is more
    specific than the bare "agent" in the agent-creation header.

    Example: "Create a new agent to schedule a daily task" — the
    user probably wants automation (matches via structural) but the
    header says "agent". The intent table ordering pins the
    automation-setup template as the more specific match when the
    structural lines are present.
    """
    msg = (
        "Create a new Automation task:\n"
        "- Name: foo\n- Schedule: 0 8 * * *\n- Output format: html"
    )
    detected = _detect_system_agent_intent(msg)
    assert detected is not None
    assert detected[0] == "automation_setup"
    assert detected[1] == "automation_agent"


def test_priority_no_overlap_disjoint_nouns() -> None:
    """The two creation-noun patterns (agent / skill) must NEVER
    overlap on the same message (so the priority table doesn't
    accidentally route to the wrong agent).
    """
    # Header uses 'agent' → agent_creation (not skill_creation)
    detected = _detect_system_agent_intent(
        "Create a new agent for code review"
    )
    assert detected[0] == "agent_creation"

    # Same root verb, different noun → skill_creation
    detected = _detect_system_agent_intent(
        "Create a new skill for code review"
    )
    assert detected[0] == "skill_creation"


# ── negative: no intent detected ────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        # Random data queries — must NOT trigger any intent.
        "Generate a Q2 sales report",
        "Show me last 30 days of inventory",
        "帮我查一下昨天的销售数据",
        "Forecast crude oil for next week",
        # Empty / None
        "",
        None,
        "1",  # bare number
        # "create" + noun that is NOT a dedicated system-agent purpose.
        # Must NOT misfire on agent_builder or skill_agent.
        "Create a new dashboard for Q2 sales",
        "Create a new report on inventory turnover",
        "Build me a presentation deck",
        "Make me a flowchart",
        # "create" without the dedicated noun.
        "create",  # just the verb
    ],
)
def test_intent_not_detected(message) -> None:
    assert _detect_system_agent_intent(message) is None, repr(message)


# ── backward-compat shims (legacy API still works) ──────────────────────


def test_shim_automation_setup_still_returns_bool() -> None:
    """The deprecated ``_detect_automation_setup_intent`` shim must
    still return ``bool`` so older callers / tests don't break.
    """
    assert _detect_automation_setup_intent(
        "Create a new Automation Task:\n- Name: x"
    ) is True
    assert _detect_automation_setup_intent(
        "Build me a presentation"
    ) is False
    # An agent-creation message is NOT automation-setup (shim returns False).
    assert _detect_automation_setup_intent(
        "Create a new agent for sales"
    ) is False


def test_shim_apply_automation_setup_routing_returns_bool() -> None:
    """The deprecated ``_apply_automation_setup_routing`` shim must
    still return ``bool`` (True iff a rebind happened AND it was to
    automation_agent).
    """
    from unittest.mock import MagicMock

    conv = MagicMock()
    conv.agent_name = "general_assistant"  # not automation_agent
    conv.id = "test-conv"
    db = MagicMock()
    db.commit = MagicMock()

    # Automation-setup message → shim returns True (rebind to automation_agent)
    assert _apply_automation_setup_routing(
        conv,
        "Create a new Automation Task:\n- Name: X",
        "user",
        db,
    ) is True
    assert conv.agent_name == "automation_agent"

    # Agent-creation message → shim returns False (rebind to agent_builder, NOT automation)
    conv2 = MagicMock()
    conv2.agent_name = "ecisco_bi_assistant"
    conv2.id = "test-conv-2"
    db2 = MagicMock()
    db2.commit = MagicMock()
    assert _apply_automation_setup_routing(
        conv2,
        "Create a new AI agent for sales",
        "user",
        db2,
    ) is False
    assert conv2.agent_name == "agent_builder"


# ── guard: structure regex for skill/agent creation not invented ───────


def test_no_structure_regex_for_agent_or_skill() -> None:
    """An intentional design choice: agent_creation and skill_creation
    only have header regexes, not structural ones. Pin this so a future
    refactor doesn't add a structural pattern that risks false positives
    (vs automation_setup, whose structural signature is highly specific).
    """
    for intent_name, _target, _header, structure in _SYSTEM_AGENT_INTENT_TABLE:
        if intent_name == "automation_setup":
            assert structure is not None, (
                "automation_setup MUST keep its structural signature"
            )
        else:
            assert structure is None, (
                f"{intent_name} should not have a structure regex "
                "(header-only is intentional to avoid false positives)"
            )


# ── guard: header regex patterns exist and are different ────────────────


def test_each_intent_has_unique_header_regex() -> None:
    """The three header regexes must be distinct objects so a refactor
    that accidentally aliases one regex to another is caught.
    """
    seen: set[int] = set()
    for _intent, _target, header, _structure in _SYSTEM_AGENT_INTENT_TABLE:
        rid = id(header.pattern)
        assert rid not in seen, (
            "Duplicate header regex pattern detected — refactor alias risk"
        )
        seen.add(rid)


def test_module_exposes_agent_creation_and_skill_regexes() -> None:
    """Pin that the helper regexes for agent/skill creation are
    importable from ``app.routers.agents`` so future tests can
    introspect them directly.
    """
    assert _AGENT_CREATION_HEADER_RE is not None
    assert _SKILL_CREATION_HEADER_RE is not None
    # The agent-creation regex does NOT match a pure skill-only header.
    assert _AGENT_CREATION_HEADER_RE.search(
        "Create a new skill for code review"
    ) is None
    # The skill-creation regex does NOT match a pure agent header.
    assert _SKILL_CREATION_HEADER_RE.search(
        "Create a new agent for code review"
    ) is None