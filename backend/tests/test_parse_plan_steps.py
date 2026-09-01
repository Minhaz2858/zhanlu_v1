"""2026-08-25: live-streaming spec — heuristic plan-step parser."""
from app.services.agent_loop.streaming_helpers import parse_plan_steps_from_text


def test_parses_numbered_list():
    text = "Let me plan this:\n1. Query sales data\n2. Compute totals\n3. Format report"
    steps = parse_plan_steps_from_text(text)
    assert len(steps) == 3
    assert steps[0]["title"] == "Query sales data"
    assert steps[1]["title"] == "Compute totals"
    assert steps[2]["title"] == "Format report"


def test_parses_step_keywords():
    text = "Step 1: Find users\nStep 2: Filter by date"
    steps = parse_plan_steps_from_text(text)
    assert len(steps) == 2
    assert steps[0]["title"] == "Find users"
    assert steps[1]["title"] == "Filter by date"


def test_parses_dash_list():
    text = "- First thing to do\n- Second thing to do"
    steps = parse_plan_steps_from_text(text)
    assert len(steps) == 2
    assert "First thing" in steps[0]["title"]


def test_parses_chinese_step():
    text = "步骤 1: 查找用户\n步骤 2: 过滤数据"
    steps = parse_plan_steps_from_text(text)
    assert len(steps) == 2


def test_returns_empty_for_no_plan():
    text = "Just answer the question directly"
    assert parse_plan_steps_from_text(text) == []


def test_returns_empty_for_empty_string():
    assert parse_plan_steps_from_text("") == []


def test_truncates_very_long_titles():
    text = "1. " + ("x" * 300)
    steps = parse_plan_steps_from_text(text)
    assert len(steps) == 1
    assert len(steps[0]["title"]) <= 210  # 200 chars + ellipsis


# ── 2026-08-27: markdown-artifact guard ─────────────────────────────────────
# The local LLM streams markdown plans with bold wrappers. Mid-stream partial
# lines ("- **") used to be captured as step titles of exactly "**", which then
# overwrote the deterministic turn-plan titles in the UI checklist (the "Plan
# 0/10 ** ** ** Revenue **" bug). These tests pin the cleanup.

def test_strips_markdown_bold_wrappers():
    text = "- **Query the data source**\n- **Build the dashboard**"
    steps = parse_plan_steps_from_text(text)
    assert len(steps) == 2
    assert steps[0]["title"] == "Query the data source"
    assert steps[1]["title"] == "Build the dashboard"


def test_numbered_bold_lines_clean():
    text = "1. **Analyze the request**\n2. **Inspect schema**"
    steps = parse_plan_steps_from_text(text)
    assert [s["title"] for s in steps] == ["Analyze the request", "Inspect schema"]


def test_bold_artifact_lines_are_skipped():
    # Mid-stream partial lines and pure bold markers must NOT become steps.
    text = "- **\n- **\n- Revenue\n- **"
    steps = parse_plan_steps_from_text(text)
    assert len(steps) == 1
    assert steps[0]["title"] == "Revenue"


def test_underscore_wrappers_stripped():
    text = "- __Gather revenue__\n- _orders_"
    steps = parse_plan_steps_from_text(text)
    assert [s["title"] for s in steps] == ["Gather revenue", "orders"]
