"""Tests for the execute_automation chat-agent tool.

The dispatcher's ``trigger_now`` is monkey-patched so the tests only verify
the tool's logic (validation, ownership, response shape, name resolution,
timeout-on-running). The real execution is exercised in production / the
existing dispatcher integration tests.
"""
import asyncio
import uuid

from app.database import SessionLocal
from app.models.automation_task import AutomationTask
from app.models.user import User
from app.services.automation_chat_tool import execute_automation_tool


def _run(coro):
    """Bridge async tool into sync tests."""
    return asyncio.run(coro)


_UID = uuid.uuid4().hex[:8]


def _e(suffix):
    return f"{suffix}-{_UID}@x.com"


def _make_task(db, owner_id, name="Weekly report"):
    t = AutomationTask(
        id=f"tsk-{uuid.uuid4().hex[:12]}",
        name=name,
        type="custom",
        prompt="summarise",
        schedule="weekly",
        status="active",
        created_by_id=owner_id,
        org_id="default-org",
        app_id="default-app",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _patch_trigger_now(monkeypatch, execution_id="exec-fake-123"):
    """Replace the dispatcher's trigger_now with a stub returning a fake id.

    Accepts ``parent_execution_id`` to match the real signature (the
    execute_automation recursion guard passes it through)."""
    async def fake_trigger_now(task_id, parent_execution_id=None):  # noqa: ARG001
        return execution_id
    monkeypatch.setattr(
        "app.services.automation_dispatcher.trigger_now",
        fake_trigger_now,
    )


def test_execute_automation_runs_task_and_returns_output(monkeypatch):
    """Happy path: an authorized owner can run their own task."""
    _patch_trigger_now(monkeypatch)
    monkeypatch.setattr(
        "app.services.automation_chat_tool._poll_execution_status",
        lambda db, eid, timeout=5.0: {"status": "completed", "output_text": "ok"},
    )
    db = SessionLocal()
    try:
        owner = User(id="u-owner", email=_e("o"), full_name="Owner",
                     password_hash="x", role="user")
        db.add(owner)
        db.commit()
        task = _make_task(db, "u-owner")
        result = _run(execute_automation_tool(
            {"task_id": task.id}, db, owner.id,
        ))
        assert result["success"] is True
        assert result["execution_id"] == "exec-fake-123"
        assert result["status"] == "completed"
        db.delete(task)
        db.commit()
    finally:
        db.close()


def test_execute_automation_denies_other_users_task(monkeypatch):
    """A different user (not the owner) must be rejected."""
    _patch_trigger_now(monkeypatch)
    db = SessionLocal()
    try:
        owner = User(id="u-owner-2", email=_e("o2"), full_name="Owner2",
                     password_hash="x", role="user")
        attacker = User(id="u-attacker", email=_e("a"), full_name="Attacker",
                        password_hash="x", role="user")
        db.add_all([owner, attacker])
        db.commit()
        task = _make_task(db, "u-owner-2")
        result = _run(execute_automation_tool(
            {"task_id": task.id}, db, attacker.id,
        ))
        assert result["success"] is False
        msg = result["error"].lower()
        assert "task" in msg and "yours" in msg, f"got {result}"
        db.delete(task)
        db.commit()
    finally:
        db.close()


def test_execute_automation_resolves_by_name_when_no_id(monkeypatch):
    """When task_id is missing, fall back to a case-insensitive name match."""
    _patch_trigger_now(monkeypatch)
    monkeypatch.setattr(
        "app.services.automation_chat_tool._poll_execution_status",
        lambda db, eid, timeout=5.0: {"status": "completed", "output_text": "ok"},
    )
    db = SessionLocal()
    try:
        owner = User(id="u-owner-3", email=_e("o3"), full_name="Owner3",
                     password_hash="x", role="user")
        db.add(owner)
        db.commit()
        task = _make_task(db, "u-owner-3", name="Daily Sales Data Sync")
        result = _run(execute_automation_tool(
            {"name": "daily sales"}, db, owner.id,
        ))
        assert result["success"] is True
        assert "execution_id" in result
        db.delete(task)
        db.commit()
    finally:
        db.close()


def test_execute_automation_returns_running_after_timeout(monkeypatch):
    """If the run hasn't finished in 5 s, return running."""
    _patch_trigger_now(monkeypatch)
    db = SessionLocal()
    try:
        owner = User(id="u-owner-4", email=_e("o4"), full_name="Owner4",
                     password_hash="x", role="user")
        db.add(owner)
        db.commit()
        task = _make_task(db, "u-owner-4")

        def fake_poll(db, execution_id, timeout=5.0):  # noqa: ARG001
            return {"status": "running"}

        monkeypatch.setattr(
            "app.services.automation_chat_tool._poll_execution_status",
            fake_poll,
        )
        result = _run(execute_automation_tool(
            {"task_id": task.id}, db, owner.id,
        ))
        assert result["success"] is True
        assert result["status"] == "running"
        db.delete(task)
        db.commit()
    finally:
        db.close()
