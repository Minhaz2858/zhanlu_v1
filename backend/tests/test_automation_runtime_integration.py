"""End-to-end smoke test for the automation runtime agent subsystem.

Verifies the full flow: create a task via _create_automation → resolve its
executor via _resolve_agent → verify it's the runtime agent → verify the
per-project memory conversation is created.
"""
from app.database import SessionLocal
from app.models.automation_task import AutomationTask
from app.models.agent_app import AgentApp
from app.services.agent_tools import _create_automation, TOOL_CONTEXT
from app.services.automation_executor import _resolve_agent
from app.services.automation_runtime import (
    ensure_automation_runtime_agent,
    get_or_create_project_conversation,
)


def test_full_create_resolve_memory_flow():
    """Create a task → resolve its executor → verify runtime agent + memory."""
    db = SessionLocal()
    try:
        org, app = "e2e-org", "e2e-app"
        # Ensure no chat agent leaks into the executor slot.
        TOOL_CONTEXT.pop("agent_app_id", None)
        result = _create_automation(
            {"name": "e2e test", "type": "custom", "prompt": "weekly sales",
             "schedule": "0 9 * * 1", "project_id": None,
             "org_id": org, "app_id": app},
            db, user_id=None,
        )
        assert result["success"] is True

        task = db.query(AutomationTask).filter(
            AutomationTask.id == result["id"]
        ).first()
        # 1. agent_id is bound (not NULL, not the chat agent)
        assert task.agent_id is not None

        # 2. _resolve_agent returns the runtime agent
        agent, reason = _resolve_agent(db, task)
        assert agent is not None
        assert reason == "ok"
        assert agent.role == "automation_runtime"
        assert agent.is_system is True

        # 3. Per-project memory conversation exists
        conv = get_or_create_project_conversation(db, agent, task.project_id)
        assert conv is not None

        # cleanup
        db.delete(task)
        db.delete(conv)
        db.delete(agent)
        db.commit()
    finally:
        TOOL_CONTEXT.pop("agent_app_id", None)
        db.close()
