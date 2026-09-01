"""Webhook trigger + trigger-source helpers (workflows router)."""

from types import SimpleNamespace

from app.routers.workflows import _find_webhook_node, _trigger_source


def _agent(flow):
    return SimpleNamespace(flow=flow)


def test_trigger_source_maps_types():
    assert _trigger_source(_agent({"nodes": [{"type": "trigger_chat"}]})) == "chat"
    assert _trigger_source(_agent({"nodes": [{"type": "trigger_manual"}]})) == "manual"
    assert _trigger_source(_agent({"nodes": [{"type": "trigger_schedule"}]})) == "schedule"
    assert _trigger_source(_agent({"nodes": [{"type": "trigger_webhook"}]})) == "webhook"
    assert _trigger_source(_agent({"nodes": [{"type": "llm"}]})) == "chat"


def test_find_webhook_node_returns_node_with_config():
    agent = _agent({"nodes": [
        {"id": "n1", "type": "trigger_webhook", "data": {"config": {"token": "sekrit"}}},
        {"id": "n2", "type": "llm", "data": {"config": {}}},
    ]})
    node = _find_webhook_node(agent)
    assert node is not None
    assert node["data"]["config"]["token"] == "sekrit"


def test_find_webhook_node_none_when_absent():
    agent = _agent({"nodes": [{"id": "n2", "type": "llm", "data": {"config": {}}}]})
    assert _find_webhook_node(agent) is None
