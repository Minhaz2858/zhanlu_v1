from app.routers.agents import _detect_system_agent_intent


def test_workflow_creation_intent_en():
    assert _detect_system_agent_intent("create a workflow that checks open orders") == ("workflow_creation", "workflow_builder")


def test_workflow_creation_intent_zh():
    assert _detect_system_agent_intent("帮我做一个工作流，每天汇总销售") == ("workflow_creation", "workflow_builder")


def test_agent_creation_still_routes_to_agent_builder():
    assert _detect_system_agent_intent("create a new agent for support") == ("agent_creation", "agent_builder")
