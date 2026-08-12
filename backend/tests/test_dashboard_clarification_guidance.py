"""Source-inspect guard for the §5.5 agent clarification gate.

Asserts the agent-facing guidance tells the agent to clarify datasource / time
window / metrics / dimensions / chart intent before building (and NOT to guess),
and that the widget options docs cover the new fields. Catches silent regression
of the clarification contract in both the tool description and the skill body.
"""


def test_tool_description_has_clarification_contract():
    from app.services.tool_handlers.dashboard_tools import CREATE_DASHBOARD_SCHEMA
    desc = CREATE_DASHBOARD_SCHEMA["function"]["description"].lower()
    for keyword in ["datasource", "time window", "metrics", "dimensions", "chart", "do not guess"]:
        assert keyword in desc, f"tool description missing '{keyword}'"


def test_skill_md_has_clarify_before_building():
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "skills" / "dashboard-generation" / "SKILL.md"
    body = p.read_text(encoding="utf-8").lower()
    assert "clarify before building" in body
    for keyword in ["datasource", "time window", "metrics", "dimensions", "chart intent"]:
        assert keyword in body, f"SKILL.md missing '{keyword}'"


def test_skill_md_limits_exploration_and_requires_create_dashboard():
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "skills" / "dashboard-generation" / "SKILL.md"
    body = p.read_text(encoding="utf-8")
    assert "Do not call `ask_data_agent` for dashboard creation" in body
    assert "At most two exploratory `execute_query` calls" in body
    assert "call `create_dashboard` before the tool loop budget is exhausted" in body


def test_template_skill_md_has_clarify_before_building():
    import pytest
    from pathlib import Path
    p = (Path(__file__).resolve().parents[1].parent
         / "docs" / "starter_templates" / "skill_library" / "system" / "visualization"
         / "dashboard-generation" / "SKILL.md")
    if not p.exists():
        pytest.skip("starter template dashboard-generation skill is not present in this checkout")
    body = p.read_text(encoding="utf-8").lower()
    assert "clarify before building" in body


def test_options_description_documents_new_fields():
    from app.services.tool_handlers.dashboard_tools import CREATE_DASHBOARD_SCHEMA
    desc = CREATE_DASHBOARD_SCHEMA["function"]["parameters"]["properties"]["widgets"]["items"]["properties"]["options"]["description"].lower()
    for keyword in ["thresholds", "drill", "dimensions", ":from", ":dim_"]:
        assert keyword in desc, f"options.description missing '{keyword}'"
