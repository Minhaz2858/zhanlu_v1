"""Unit tests for the automation runtime agent subsystem."""
import pytest
from sqlalchemy import inspect

from app.database import SessionLocal, engine
from app.models.agent_app import AgentApp


def test_agent_app_has_role_column():
    """The AgentApp table must have a nullable 'role' column."""
    inspector = inspect(engine)
    cols = {c["name"]: c for c in inspector.get_columns("agent_apps")}
    assert "role" in cols, "agent_apps table is missing the 'role' column"
    assert cols["role"]["nullable"] is True


def test_role_column_has_index():
    """The 'role' column should be indexed for fast filtering."""
    inspector = inspect(engine)
    indexes = inspector.get_indexes("agent_apps")
    index_cols = {tuple(ix["column_names"]) for ix in indexes}
    assert ("role",) in index_cols, "agent_apps.role should have an index"


def test_new_agent_role_defaults_to_none():
    """A freshly created user agent has role = None."""
    db = SessionLocal()
    try:
        a = AgentApp(name="test_role_agent", is_system=False, status="draft")
        db.add(a)
        db.commit()
        db.refresh(a)
        assert a.role is None
        db.delete(a)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Task 2: ensure_automation_runtime_agent
# ---------------------------------------------------------------------------
from app.services.automation_runtime import ensure_automation_runtime_agent


def test_ensure_runtime_agent_is_idempotent():
    """Calling twice with the same (org, app) returns the same record."""
    db = SessionLocal()
    try:
        a1 = ensure_automation_runtime_agent(db, "test-org", "test-app")
        a2 = ensure_automation_runtime_agent(db, "test-org", "test-app")
        assert a1.id == a2.id
        assert a1.name == "automation_runtime_agent"
        assert a1.is_system is True
        assert a1.role == "automation_runtime"
        assert a1.status == "active"
        assert a1.org_id == "test-org"
        assert a1.app_id == "test-app"
        # cleanup
        db.delete(a1)
        db.commit()
    finally:
        db.close()


def test_ensure_runtime_agent_separate_orgs_get_separate_agents():
    """Different (org, app) pairs get different runtime agents."""
    db = SessionLocal()
    try:
        a1 = ensure_automation_runtime_agent(db, "org-a", "app-a")
        a2 = ensure_automation_runtime_agent(db, "org-b", "app-b")
        assert a1.id != a2.id
        db.delete(a1)
        db.delete(a2)
        db.commit()
    finally:
        db.close()


def test_runtime_agent_tool_whitelist_excludes_automation_crud():
    """The runtime agent must NOT be able to create/update/delete automations."""
    db = SessionLocal()
    try:
        a = ensure_automation_runtime_agent(db, "test-org-wl", "test-app-wl")
        enabled = set((a.tool_config or {}).get("enabled_tools", []))
        forbidden = {"create_automation", "update_automation", "delete_automation",
                     "create_agent", "delete_agent"}
        assert not (enabled & forbidden), f"runtime agent has forbidden tools: {enabled & forbidden}"
        # Must have the 4 allowed categories
        assert "web_search" in enabled
        assert "create_artifact" in enabled
        assert "send_message" in enabled
        db.delete(a)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Task 3: backfill_automation_runtime_agents
# ---------------------------------------------------------------------------
from app.models.automation_task import AutomationTask
from app.services.automation_runtime import backfill_automation_runtime_agents


def test_backfill_rebinds_null_agent_id():
    """Tasks with agent_id=NULL get rebound to the runtime agent."""
    db = SessionLocal()
    try:
        org, app = "backfill-org", "backfill-app"
        t = AutomationTask(
            name="test backfill", type="custom", prompt="hi",
            schedule="manual", status="paused",
            agent_id=None, org_id=org, app_id=app,
        )
        db.add(t)
        db.commit()
        db.refresh(t)
        assert t.agent_id is None

        rebound = backfill_automation_runtime_agents(db)
        assert rebound >= 1
        db.refresh(t)
        runtime = ensure_automation_runtime_agent(db, org, app)
        assert t.agent_id == runtime.id
        # cleanup
        db.delete(t)
        db.delete(runtime)
        db.commit()
    finally:
        db.close()


def test_backfill_preserves_valid_user_agent_id():
    """Tasks with a valid non-system agent_id are left alone."""
    db = SessionLocal()
    try:
        org, app = "preserve-org", "preserve-app"
        user_agent = AgentApp(name="my_user_agent", is_system=False, status="active",
                              org_id=org, app_id=app)
        db.add(user_agent)
        db.commit()
        db.refresh(user_agent)
        t = AutomationTask(
            name="test preserve", type="custom", prompt="hi",
            schedule="manual", status="paused",
            agent_id=user_agent.id, org_id=org, app_id=app,
        )
        db.add(t)
        db.commit()
        db.refresh(t)

        backfill_automation_runtime_agents(db)
        db.refresh(t)
        assert t.agent_id == user_agent.id  # unchanged
        db.delete(t)
        db.delete(user_agent)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Task 4: _create_automation auto-binds runtime agent
# ---------------------------------------------------------------------------
from app.services.agent_tools import _create_automation, TOOL_CONTEXT


def test_create_automation_binds_runtime_agent_not_chat_agent():
    """_create_automation must default agent_id to the runtime agent,
    NOT to the chat agent from TOOL_CONTEXT."""
    db = SessionLocal()
    try:
        org, app = "create-org", "create-app"
        # Simulate a chat agent in context — it must NOT become the executor.
        chat_agent = AgentApp(name="chat_agent_test", is_system=False, status="active",
                              org_id=org, app_id=app)
        db.add(chat_agent)
        db.commit()
        db.refresh(chat_agent)
        TOOL_CONTEXT["agent_app_id"] = chat_agent.id
        TOOL_CONTEXT["chat_session_id"] = None

        result = _create_automation(
            {"name": "auto-bind test", "type": "custom", "prompt": "hi",
             "schedule": "manual", "project_id": None,
             "org_id": org, "app_id": app},
            db, user_id=None,
        )
        assert result["success"] is True
        task = db.query(AutomationTask).filter(
            AutomationTask.id == result["id"]
        ).first()
        runtime = ensure_automation_runtime_agent(db, org, app)
        assert task.agent_id == runtime.id
        assert task.agent_id != chat_agent.id
        # cleanup
        db.delete(task)
        db.delete(chat_agent)
        db.delete(runtime)
        db.commit()
    finally:
        TOOL_CONTEXT.pop("agent_app_id", None)
        db.close()


# ---------------------------------------------------------------------------
# Task 5: _resolve_agent runtime fallback
# ---------------------------------------------------------------------------
from app.services.automation_executor import _resolve_agent


def test_resolve_agent_falls_back_to_runtime_when_agent_id_null():
    """When task.agent_id is NULL, _resolve_agent returns the runtime agent."""
    db = SessionLocal()
    try:
        org, app = "resolve-org", "resolve-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        t = AutomationTask(
            name="resolve test", type="custom", prompt="hi",
            schedule="manual", status="paused",
            agent_id=None, org_id=org, app_id=app,
        )
        db.add(t)
        db.commit()
        db.refresh(t)

        agent, reason = _resolve_agent(db, t)
        assert agent is not None
        assert agent.id == runtime.id
        assert reason == "ok"
        # cleanup
        db.delete(t)
        db.delete(runtime)
        db.commit()
    finally:
        db.close()


def test_resolve_agent_falls_back_to_runtime_when_pinned_agent_deleted():
    """When task.agent_id points at a deleted/missing agent, fall back to runtime."""
    db = SessionLocal()
    try:
        org, app = "resolve-del-org", "resolve-del-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        t = AutomationTask(
            name="resolve deleted test", type="custom", prompt="hi",
            schedule="manual", status="paused",
            agent_id="nonexistent-agent-id", org_id=org, app_id=app,
        )
        db.add(t)
        db.commit()
        db.refresh(t)

        agent, reason = _resolve_agent(db, t)
        assert agent is not None
        assert agent.id == runtime.id
        # cleanup
        db.delete(t)
        db.delete(runtime)
        db.commit()
    finally:
        db.close()


def test_resolve_agent_returns_runtime_when_multiple_workspace_agents():
    """The real bug: when multiple agents exist in the workspace and none is
    pinned, the OLD code failed with 'N agents exist'. The fix must
    deterministically return the runtime agent instead of guessing or failing.
    """
    db = SessionLocal()
    try:
        org, app = "multi-org", "multi-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        # Create TWO extra user agents so the workspace has >1 candidate.
        u1 = AgentApp(name="user_a_multi", is_system=False, status="active",
                      org_id=org, app_id=app)
        u2 = AgentApp(name="user_b_multi", is_system=False, status="active",
                      org_id=org, app_id=app)
        db.add_all([u1, u2])
        db.commit()
        t = AutomationTask(
            name="multi resolve test", type="custom", prompt="hi",
            schedule="manual", status="paused",
            agent_id=None, org_id=org, app_id=app,
        )
        db.add(t)
        db.commit()
        db.refresh(t)

        agent, reason = _resolve_agent(db, t)
        assert agent is not None, "should not fail when multiple agents exist"
        assert reason == "ok"
        assert agent.id == runtime.id  # deterministically the runtime agent
        # cleanup
        db.delete(t)
        db.delete(u1)
        db.delete(u2)
        db.delete(runtime)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Task 6: _is_transient_error classification
# ---------------------------------------------------------------------------
from app.services.automation_executor import _is_transient_error


def test_is_transient_http_5xx():
    """A 503-style error is transient."""
    assert _is_transient_error(Exception("server returned 503 service unavailable"))


def test_is_transient_timeout():
    """A timeout error is transient."""
    assert _is_transient_error(Exception("connection timed out after 30s"))


def test_is_transient_rate_limit():
    """A rate-limit error is transient."""
    assert _is_transient_error(Exception("429 rate limit exceeded"))


def test_not_transient_validation_error():
    """A validation/4xx error is NOT transient — retrying wastes budget."""
    assert not _is_transient_error(ValueError("invalid prompt: empty"))


def test_not_transient_permission_denied():
    """A 403 permission error is NOT transient."""
    assert not _is_transient_error(PermissionError("403 forbidden: no access"))


# ---------------------------------------------------------------------------
# Task 7: Per-project memory conversation ledger
# ---------------------------------------------------------------------------
from app.services.automation_runtime import (
    get_or_create_project_conversation, append_run_summary,
)


def test_project_conversation_is_idempotent():
    """Repeated calls return the same conversation for the same project."""
    db = SessionLocal()
    try:
        org, app = "mem-org", "mem-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        c1 = get_or_create_project_conversation(db, runtime, None)
        c2 = get_or_create_project_conversation(db, runtime, None)
        assert c1.id == c2.id
        db.delete(c1)
        db.delete(runtime)
        db.commit()
    finally:
        db.close()


def test_different_projects_get_different_conversations():
    """Different project_ids get different conversations."""
    from app.models.project import Project
    db = SessionLocal()
    try:
        org, app = "mem2-org", "mem2-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        p1 = Project(name="proj-a", org_id=org, app_id=app)
        p2 = Project(name="proj-b", org_id=org, app_id=app)
        db.add_all([p1, p2])
        db.commit()
        db.refresh(p1)
        db.refresh(p2)
        c1 = get_or_create_project_conversation(db, runtime, p1.id)
        c2 = get_or_create_project_conversation(db, runtime, p2.id)
        assert c1.id != c2.id
        db.delete(c1)
        db.delete(c2)
        db.delete(runtime)
        db.delete(p1)
        db.delete(p2)
        db.commit()
    finally:
        db.close()


def test_append_run_summary_grows_messages():
    """append_run_summary adds a ledger entry to the conversation."""
    db = SessionLocal()
    try:
        org, app = "mem3-org", "mem3-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        conv = get_or_create_project_conversation(db, runtime, None)
        before = len(conv.messages or [])
        append_run_summary(db, conv, "run-1", "ok", "Sales up 12%")
        db.refresh(conv)
        after = len(conv.messages or [])
        assert after == before + 1
        db.delete(conv)
        db.delete(runtime)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Task 8: runtime agent excluded from user listings (regression guard)
# ---------------------------------------------------------------------------
def test_runtime_agent_is_excluded_from_user_listing():
    """A direct query for user-visible agents must not return the runtime agent.

    The runtime agent has is_system=True, so the is_system == False filter
    already excludes it. This test is a regression guard.
    """
    db = SessionLocal()
    try:
        org, app = "hide-org", "hide-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        visible = db.query(AgentApp).filter(
            AgentApp.org_id == org,
            AgentApp.app_id == app,
            AgentApp.is_deleted == False,  # noqa: E712
            AgentApp.is_system == False,  # noqa: E712
        ).all()
        assert runtime.id not in {a.id for a in visible}
        db.delete(runtime)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Task 9: admin diagnostic query (role-based lookup)
# ---------------------------------------------------------------------------
def test_runtime_agent_has_role_for_diagnostics():
    """The runtime agent carries role='automation_runtime' so the admin
    diagnostic endpoint can find it."""
    db = SessionLocal()
    try:
        org, app = "diag-org", "diag-app"
        runtime = ensure_automation_runtime_agent(db, org, app)
        assert runtime.role == "automation_runtime"
        runtimes = db.query(AgentApp).filter(
            AgentApp.role == "automation_runtime",
            AgentApp.is_deleted == False,  # noqa: E712
        ).all()
        assert runtime.id in {a.id for a in runtimes}
        db.delete(runtime)
        db.commit()
    finally:
        db.close()
