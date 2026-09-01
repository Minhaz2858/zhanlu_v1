from app.services.system_agents import _build_system_agent_configs


def test_workflow_builder_config_present():
    names = [c["name"] for c in _build_system_agent_configs()]
    assert "workflow_builder" in names
    cfg = next(c for c in _build_system_agent_configs() if c["name"] == "workflow_builder")
    assert cfg["is_system"] is True
    assert "flow_mode" not in cfg or cfg["flow_mode"] is False  # must be a REGULAR agent


def test_workflow_builder_has_no_tools():
    """The builder must NEVER hold tools — create_artifact trips the v3
    deliverable phase-lock (blocked before data collection) and makes the
    agent burn its budget producing artifacts instead of the
    decision-summary block."""
    cfg = next(c for c in _build_system_agent_configs() if c["name"] == "workflow_builder")
    tools = cfg["tool_config"]["enabled_tools"]
    assert tools == []
    assert "create_artifact" not in tools
    assert "list_tools" not in tools
