"""Phase 10 / W5+S3.2 — central dispatch authorization boundaries.

* S5.2: the agent tool-policy gate must FAIL CLOSED — a policy-layer
  fault denies the tool instead of silently allowing everything.
* S3.2: the caller's org is server-resolved context; a client/LLM
  supplied ``org_id`` tool argument must never override it.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_policy_gate_failure_denies_tool(monkeypatch):
    """When the tool policy cannot be LOADED, the invocation is denied —
    never executed unscoped."""
    from app.services import agent_tools

    def boom(db, agent_app_id):
        raise RuntimeError("policy store unavailable")

    monkeypatch.setattr("app.services.agent_tool_policy.load_tool_policy",
                        boom)
    result = await agent_tools.execute_tool(
        "list_data_sources", {}, db=None, user_id="u1",
        context={"agent_app_id": "app-1"},
    )
    assert result["success"] is False
    assert "could not be verified" in (result.get("error") or "")


def test_org_override_stripped_from_tool_args(monkeypatch):
    """args['org_id'] is client-controlled; the automation executor-agent
    resolution must use the server-resolved TOOL_CONTEXT org, not the
    argument."""
    from app.services import agent_tools

    captured = {}

    def fake_ensure(db, org_id, app_id):
        captured["org_id"] = org_id
        return SimpleNamespace(id="agent-runtime")

    monkeypatch.setattr(
        "app.services.automation_runtime.ensure_automation_runtime_agent",
        fake_ensure)

    monkeypatch.setattr(agent_tools, "TOOL_CONTEXT",
                        {"org_id": "org-server", "app_id": "app-server"})
    agent_tools._resolve_task_executor_agent(
        None, {"org_id": "org-EVIL", "app_id": "app-x"})
    assert captured["org_id"] == "org-server", (
        "client-supplied org_id overrode the server-resolved context"
    )
