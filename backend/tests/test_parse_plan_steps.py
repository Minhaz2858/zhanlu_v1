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
