"""Regression tests for the 2026-08-25 third-pass bug.

User pasted "Create a new Automation Task: ... Sync ERP sales data..."
into a chat bound to project Ecisco BI. The conversation ran on
``ecisco_bi_assistant`` (which has ``ask_data_agent`` but NOT
``create_automation`` — only ``automation_agent`` has that tool). The
LLM misread the description as a data-analysis request and called
``ask_data_agent``, which returned 1 row with 1 column (a placeholder
count) and emitted "no business-meaningful columns found: 1." — the
exact misleading wrap-up the user reported.

These tests pin the chat-routing helper that detects automation-setup
intent (EN + ZH + structural-form) and lets ``add_message`` /
``add_message_stream`` rebind the conversation to the dedicated
``automation_agent`` (which has ``create_automation`` + the right
NO-REPORT BOUNDARY prompt). Without this rebind, the conversation
stays on ``ecisco_bi_assistant`` and the LLM has no automation tool.
"""

from __future__ import annotations

import os

# Override UPLOAD_DIR before any app.* import so the settings.upload_path
# property can mkdir() in this sandbox (default /app/data/uploads is read-
# only outside the container). Mirrors how container tests resolve this
# (env_config_tool also touches upload_path on import).
os.environ.setdefault("UPLOAD_DIR", "/tmp/test_uploads_automation_setup_intent")
os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)

import pytest

from app.routers.agents import (
    _detect_automation_setup_intent,
    _AUTOMATION_SETUP_HEADER_RE,
    _AUTOMATION_SETUP_STRUCTURE_RE,
)


@pytest.mark.parametrize(
    "message",
    [
        # EN explicit header — the canonical chat composer template.
        (
            "Create a new Automation Task:\n"
            "- Name: Daily Sales Data Sync\n"
            "- Type: Data Sync\n"
            "- Schedule: Daily 08:00\n"
            "- Output format: HTML report (html)\n"
            "- Project: Ecisco BI\n"
            "- Description: Sync ERP sales data daily"
        ),
        # EN with casing typo (lowercase 'task').
        "Create a new Automation task:\n- Name: X\n- Schedule: daily",
        # EN variant: "Set up …".
        "Please set up a new automation for me.\n- Name: Q2 sales report",
        # EN variant: "Schedule a daily task …".
        "Schedule a daily task that pulls inventory data\n"
        "- Name: Inventory sync\n- Schedule: every morning",
        # ZH — the literal text from the user's most recent run.
        (
            "帮我新建一个自动化任务:\n"
            "- 名称:每日销售数据同步\n"
            "- 类型:数据同步\n"
            "- 调度规则:每天 08:00\n"
            "- 输出格式:HTML report (html)\n"
            "- 所属项目:Ecisco BI"
        ),
        # ZH with fullwidth colon
        (
            "帮我新建一个自动化任务：\n"
            "- 名称：每日销售数据同步\n- 调度规则：每天 08:00"
        ),
        # ZH variants — creation verbs.
        "新建一个自动化任务:\n...",
        "创建自动化任务:\n...",
        "添加自动化任务:\n...",
        # ZH without the header — structural only.
        (
            "- 名称:每日销售数据同步\n"
            "- 调度规则:每天 08:00\n"
            "- 输出格式:HTML report (html)"
        ),
        # EN without header — structural only (the exact shape the user pasted).
        (
            "- Name: Daily Sales Data Sync\n"
            "- Type: Data Sync\n"
            "- Schedule: Daily 08:00\n"
            "- Output format: HTML report (html)\n"
            "- Project: Ecisco BI\n"
            "- Description: Sync ERP sales data daily"
        ),
    ],
)
def test_intent_detected(message: str) -> None:
    """The full set of automation-setup templates (EN + ZH + structural)
    MUST be detected so the chat router rebinds to automation_agent.
    """
    assert _detect_automation_setup_intent(message), repr(message)


@pytest.mark.parametrize(
    "message",
    [
        # Pure data queries — must NOT trigger routing.
        "Generate a Q2 sales report",
        "Show me last 30 days of inventory",
        "帮我查一下昨天的销售数据",
        "Forecast crude oil for next week",
        "What's the price trend for cracked C5?",
        # Data intents that mention "schedule" but are NOT automation setup.
        "Schedule a meeting with the sales team at 3pm",
        # Misc conversational messages.
        "hello",
        "thanks",
        "the report looks good",
        # Empty / None inputs.
        "",
        None,
        "1",  # bare number, no structure
    ],
)
def test_intent_not_detected(message) -> None:
    """False-positive guard: regular data/chat messages must NOT be
    routed to automation_agent."""
    assert not _detect_automation_setup_intent(message), repr(message)


def test_header_regex_alone_matches_canonical() -> None:
    """Verify the HEADER regex fires on the canonical template alone."""
    assert _AUTOMATION_SETUP_HEADER_RE.search(
        "Create a new Automation Task:\n- Name: X\n- Schedule: Y"
    )


def test_structure_regex_alone_matches_header_less_template() -> None:
    """Verify the STRUCTURE regex fires on a template with no header."""
    msg = (
        "- Name: Foo\n"
        "- Schedule: daily\n"
        "- Output format: html\n"
    )
    assert _AUTOMATION_SETUP_STRUCTURE_RE.search(msg)


def test_structure_regex_requires_all_three_lines() -> None:
    """All three lines (Name + Schedule + Output format) MUST be present.

    Tests the structural regex does not over-match templates that
    lack the schedule line.
    """
    # Missing 'Output format' line — should NOT match
    assert not _AUTOMATION_SETUP_STRUCTURE_RE.search(
        "- Name: Foo\n- Schedule: daily\n"
    )
    # Missing 'Schedule' line — should NOT match
    assert not _AUTOMATION_SETUP_STRUCTURE_RE.search(
        "- Name: Foo\n- Output format: html\n"
    )
    # All three present — should match
    assert _AUTOMATION_SETUP_STRUCTURE_RE.search(
        "- Name: Foo\n- Schedule: daily\n- Output format: html\n"
    )


def test_intent_strips_whitespace_and_handles_null() -> None:
    """Whitespace-only and None content are safely detected as False."""
    assert not _detect_automation_setup_intent("   \n\t\n")
    assert not _detect_automation_setup_intent(None)
