from app.services.system_agents import _build_system_agent_configs


def test_workflow_builder_config_present():
    names = [c["name"] for c in _build_system_agent_configs()]
    assert "workflow_builder" in names
    cfg = next(c for c in _build_system_agent_configs() if c["name"] == "workflow_builder")
    assert cfg["is_system"] is True
    assert "flow_mode" not in cfg or cfg["flow_mode"] is False  # must be a REGULAR agent
