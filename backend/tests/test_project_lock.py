"""Project-lock tests: the task's project identity is resolved once and
frozen, and the dedicated chat session is reconciled to that identity.

Covers three behaviors required by the automation "Run Now" redesign:

1. ``_resolve_task_project`` PERSISTS the adopted ``project_id`` when the
   task carries only the legacy name string (FK NULL). Today it returns
   the adopted id without writing it back, so the next run re-adopts
   (and can drift to a different same-name Project).

2. Once persisted, the adoption is STABLE: a newer same-name Project
   appearing later does NOT change the task's ``project_id`` (the
   per-run latest-updated-name lookup would otherwise re-bind it).

3. ``ensure_task_chat_session`` (the new service in
   ``app.services.automation_sessions``) reconciles a session whose
   ``project_id`` / ``project`` no longer match the task's resolved
   project back to the task's project — so a session that drifted
   (e.g. created before the FK was adopted, or tagged with a sibling
   project) is corrected on the next Run Now.
"""
from __future__ import annotations

import time

from app.database import SessionLocal
from app.models.automation_task import AutomationTask
from app.models.chat_session import ChatSession
from app.models.project import Project


# --- Fixtures ---------------------------------------------------------------


def _make_project(db, name: str = "Q3 Sales") -> Project:
    proj = Project(name=name, status="active")
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _make_task(
    db,
    *,
    name: str = "Daily Sales Report",
    project_name: str | None = "Q3 Sales",
    project_id: str | None = None,
) -> AutomationTask:
    task = AutomationTask(
        name=name,
        type="scheduled",
        prompt="Summarize sales",
        cron_expression="0 9 * * *",
        project=project_name,
        project_id=project_id,
        is_deleted=False,
        notify_chat=True,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _cleanup(db, *objs) -> None:
    for o in objs:
        if o is None:
            continue
        try:
            db.refresh(o)
        except Exception:
            pass
        db.delete(o)
    db.commit()


# --- Test 1: persist adopted project_id ------------------------------------


def test_resolve_task_project_persists_adopted_fk_when_null():
    """A task with FK=NULL + legacy name must have its adopted project_id
    written back to the row (idempotent, self-contained commit)."""
    db = SessionLocal()
    proj = _make_project(db, "Persist Project A")
    task = _make_task(
        db,
        name="Task Persist A",
        project_name="Persist Project A",
        project_id=None,
    )
    try:
        assert task.project_id is None

        from app.services.automation_executor import _resolve_task_project
        adopted_id, name = _resolve_task_project(db, task)

        assert adopted_id == proj.id, "should adopt the matching Project"
        # The new behavior: the FK is now persisted on the task.
        db.refresh(task)
        assert task.project_id == proj.id, (
            "adopted project_id must be persisted on the task so later "
            "runs don't re-adopt (and drift)."
        )
    finally:
        _cleanup(db, task, proj)
        db.close()


# --- Test 2: adoption stable across newer same-name Projects ----------------


def test_resolve_task_project_adoption_stable_after_newer_same_name():
    """Once the FK is persisted, a newer same-name Project appearing
    later must NOT change the task's project_id (frozen binding)."""
    db = SessionLocal()
    proj1 = _make_project(db, "Stable Project B")
    task = _make_task(
        db,
        name="Task Stable B",
        project_name="Stable Project B",
        project_id=None,
    )
    try:
        from app.services.automation_executor import _resolve_task_project

        # First resolve: adopts proj1 and persists the FK.
        adopted_id, _ = _resolve_task_project(db, task)
        assert adopted_id == proj1.id
        db.refresh(task)
        assert task.project_id == proj1.id

        # A newer same-name Project appears (later updated_date).
        time.sleep(0.01)
        proj2 = Project(name="Stable Project B", status="active")
        db.add(proj2)
        db.commit()
        db.refresh(proj2)

        # Second resolve: must return the ALREADY-PERSISTED FK (proj1),
        # not re-adopt the newer proj2.
        adopted_id_2, _ = _resolve_task_project(db, task)
        assert adopted_id_2 == proj1.id, (
            "persisted FK must win — the binding is frozen and must not "
            "drift to a newer same-name Project."
        )
        db.refresh(task)
        assert task.project_id == proj1.id
        _cleanup(db, proj2)
    finally:
        _cleanup(db, task, proj1)
        db.close()


# --- Test 3: ensure_task_chat_session reconciles drifted project tags -------


def test_ensure_task_chat_session_reconciles_drifted_session_project():
    """A session tagged with the WRONG project (sibling/drifted) must be
    reconciled back to the task's resolved project on the next Run Now."""
    db = SessionLocal()
    proj_task = _make_project(db, "Task Project C")
    proj_other = _make_project(db, "Other Project C")
    task = _make_task(
        db,
        name="Task Reconcile C",
        project_name="Task Project C",
        project_id=None,  # force adoption so the resolved id is proj_task
    )
    try:
        # First, persist the task's project identity.
        from app.services.automation_executor import _resolve_task_project
        resolved_id, _ = _resolve_task_project(db, task)
        assert resolved_id == proj_task.id
        db.refresh(task)
        assert task.project_id == proj_task.id

        # Now hand-craft a DRIFTED session: it belongs to the task (in
        # task.session_id) but is tagged with the sibling project.
        from app.models.agent_conversation import AgentConversation
        conv = AgentConversation(
            agent_name=None,
            title=task.name,
            messages=[],
            status="active",
            project_id=proj_other.id,  # WRONG project
        )
        db.add(conv)
        db.flush()
        drifted = ChatSession(
            title=task.name,
            project_id=proj_other.id,   # WRONG
            project="Other Project C",  # WRONG
            conversation_id=conv.id,
        )
        db.add(drifted)
        db.flush()
        task.session_id = drifted.id
        db.commit()
        db.refresh(task)

        # Import the NEW service (lives in app.services.automation_sessions).
        from app.services.automation_sessions import ensure_task_chat_session
        session_id, created = ensure_task_chat_session(db, task)

        drifted_fresh = db.query(ChatSession).filter(
            ChatSession.id == drifted.id
        ).first()
        # The drifted session must now carry the TASK's project identity.
        assert drifted_fresh.project_id == proj_task.id, (
            "drifted session project_id must be reconciled to the task's "
            "resolved project."
        )
        assert drifted_fresh.project == "Task Project C", (
            "drifted session legacy project name must be reconciled too."
        )
        # No new session should have been created — the existing one is
        # corrected in place (reconciliation, not re-adoption).
        assert session_id == drifted.id
        assert created is False
    finally:
        # FK-safe cleanup order: task (clears automation_tasks.session_id)
        # → session (clears chat_sessions.conversation_id) → conv → projects
        # (projects are referenced by task.project_id + session.project_id).
        _cleanup(db, task)
        for obj in (drifted, conv, proj_task, proj_other):
            try:
                _cleanup(db, obj)
            except Exception:
                pass
        db.close()
