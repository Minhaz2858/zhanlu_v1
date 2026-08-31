"""Regression tests for create_automation placeholder-arg handling.

Root cause (2026-08-25): the user created the "Daily Sales Data Sync"
automation inside an Ecisco BI chat. The chat session has
project_id=e5ac337b-... (Ecisco BI) and conv.session_id=4b0f7a77-... — both
real UUIDs. The LLM called `create_automation` but passed
project_id="TOOL_CONTEXT.project_id" as the literal string (the prompt said
"use TOOL_CONTEXT.project_id if you don't already have it" and the LLM
echoed that placeholder verbatim instead of substituting).

The handler did `args.get("project_id") or TOOL_CONTEXT.get("project_id")`,
which kept the literal string (truthy) and bypassed the fallback chain,
producing:

    psycopg2.errors.ForeignKeyViolation) insert or update on table
    "automation_tasks" violates foreign key constraint
    "automation_tasks_project_id_fkey"
    DETAIL: Key (project_id)=(TOOL_CONTEXT.project_id) is not present in
            table "projects".

The fix: only accept a project_id arg if it is UUID-shaped; otherwise log
a warning and fall back to TOOL_CONTEXT → session lookup → name
resolution. The same guard protects session_id.

These tests pin both behaviors. They do NOT exercise the schedule parser
or cron path — see test_automation_create_status_validation.py for that.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.database import SessionLocal, engine
from app.models.base import Base
from app.models.automation_task import AutomationTask
from app.models.chat_session import ChatSession
from app.models.agent_conversation import AgentConversation
from app.models.project import Project
from app.services.agent_tools import (
    TOOL_CONTEXT,
    _create_automation,
    _looks_like_llm_placeholder,
    _looks_like_uuid,
)


@pytest.fixture(autouse=True)
def _clean_slate():
    """Fresh schema + empty automation_tasks/chat_sessions/projects per test."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    TOOL_CONTEXT.clear()


def _make_project(db, name: str = "Ecisco BI") -> Project:
    p = Project(id=str(uuid.uuid4()), name=name, is_deleted=False)
    db.add(p)
    db.flush()
    return p


def _make_chat_session(db, project_id: str, project_name: str = "Ecisco BI") -> ChatSession:
    conv = AgentConversation(
        id=str(uuid.uuid4()),
        title="t",
        messages=[],
        status="active",
        project_id=project_id,
    )
    db.add(conv)
    db.flush()
    sess = ChatSession(
        id=str(uuid.uuid4()),
        title="t",
        project_id=project_id,
        project=project_name,
        conversation_id=conv.id,
        agent_name=None,
    )
    db.add(sess)
    db.flush()
    return sess


# ---------------------------------------------------------------------------
# Helper-level unit tests (no DB)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "TOOL_CONTEXT.project_id",
        "TOOL_CONTEXT.chat_session_id",
        "${project_id}",
        "{{project_id}}",
        "<project_id>",
        "None",
        "none",
        "undefined",
        "null",
        "NA",
    ],
)
def test_looks_like_llm_placeholder_true(value: str) -> None:
    assert _looks_like_llm_placeholder(value), value


@pytest.mark.parametrize(
    "value",
    [
        "e5ac337b-469a-480d-822b-f6a3155e652c",  # real UUID
        "Ecisco BI",                              # legacy project name
        "kebab-case-name",
        "0",
        "",
    ],
)
def test_looks_like_llm_placeholder_false(value: str) -> None:
    assert not _looks_like_llm_placeholder(value), value


@pytest.mark.parametrize(
    "value",
    [
        "e5ac337b-469a-480d-822b-f6a3155e652c",
        "00000000-0000-0000-0000-000000000000",
        "ABCDEF12-3456-7890-ABCD-EF1234567890",
    ],
)
def test_looks_like_uuid_true(value: str) -> None:
    assert _looks_like_uuid(value), value


@pytest.mark.parametrize(
    "value",
    [
        None,
        0,
        123,
        "TOOL_CONTEXT.project_id",
        "Ecisco BI",
        "not-a-uuid",
        "e5ac337b-469a-480d-822b-f6a3155e652c ",  # trailing space still True
    ],
)
def test_looks_like_uuid_handles_edge_cases(value) -> None:
    """Whitespace-padded UUIDs are still UUIDs; everything else is not."""
    expected = (
        isinstance(value, str)
        and value.strip() == "e5ac337b-469a-480d-822b-f6a3155e652c"
    )
    assert _looks_like_uuid(value) is expected, value


# ---------------------------------------------------------------------------
# _create_automation: placeholder must NOT bypass fallback (the real bug)
# ---------------------------------------------------------------------------


def test_create_automation_rejects_placeholder_project_id() -> None:
    """The literal sentinel from the buggy chat must NOT be persisted.

    Reproduces the 2026-08-25 FK violation: LLM passed
    project_id='TOOL_CONTEXT.project_id'. With the fix, the handler MUST
    fall back to TOOL_CONTEXT['project_id'] (which we set to a real UUID)
    rather than echo the placeholder into the row.
    """
    db = SessionLocal()
    try:
        proj = _make_project(db, "Ecisco BI")
        proj_uuid = str(proj.id)
        sess = _make_chat_session(db, proj_uuid)

        TOOL_CONTEXT.clear()
        TOOL_CONTEXT["chat_session_id"] = sess.id
        TOOL_CONTEXT["project_id"] = proj_uuid  # what the chat loop should inject

        result = _create_automation(
            {
                "name": "Daily Sales Data Sync",
                "type": "data_sync",
                "schedule": "0 8 * * *",
                "project_id": "TOOL_CONTEXT.project_id",  # the LLM's bug echo
                "session_id": "TOOL_CONTEXT.chat_session_id",  # same bug, similar
                "prompt": "Sync ERP sales daily",
            },
            db=db,
            user_id="u-test",
        )
        db.commit()

        assert "id" in result
        task_id = result["id"]
        row = db.get(AutomationTask, task_id)
        assert row is not None
        # The placeholder must NOT be persisted; the context UUID must.
        assert row.project_id == proj_uuid
        assert row.session_id == sess.id
        assert row.project == "Ecisco BI"
    finally:
        db.close()


def test_create_automation_honors_real_uuid_project_id() -> None:
    """When the LLM DOES pass a real UUID (the happy path), it must win.

    Guards against over-correction: the new placeholder guard should not
    clobber valid UUID args.
    """
    db = SessionLocal()
    try:
        proj = _make_project(db, "Ecisco BI")
        explicit_uuid = str(proj.id)
        _make_project(db, "Other Project")

        TOOL_CONTEXT.clear()
        # Even if TOOL_CONTEXT points at a different project, the explicit
        # UUID arg must take precedence.
        TOOL_CONTEXT["project_id"] = "00000000-0000-0000-0000-000000000099"

        result = _create_automation(
            {
                "name": "explicit-uuid test",
                "type": "custom",
                "schedule": "manual",
                "project_id": explicit_uuid,
                "project": "Ecisco BI",
            },
            db=db,
            user_id="u-test",
        )
        db.commit()

        row = db.get(AutomationTask, result["id"])
        assert row.project_id == explicit_uuid
        assert row.project == "Ecisco BI"
    finally:
        db.close()


def test_create_automation_falls_back_to_session_when_toycontext_empty() -> None:
    """No TOOL_CONTEXT + no explicit arg → chat-session-derived project_id."""
    db = SessionLocal()
    try:
        proj = _make_project(db, "Ecisco BI")
        sess = _make_chat_session(db, str(proj.id))

        TOOL_CONTEXT.clear()
        TOOL_CONTEXT["chat_session_id"] = sess.id  # no project_id here

        result = _create_automation(
            {
                "name": "session-derived",
                "type": "custom",
                "schedule": "manual",
                # NO project_id passed
            },
            db=db,
            user_id="u-test",
        )
        db.commit()

        row = db.get(AutomationTask, result["id"])
        assert row.project_id == str(proj.id)
        # memoize must have happened
        assert TOOL_CONTEXT.get("project_id") == str(proj.id)
    finally:
        db.close()


def test_create_automation_rejects_placeholder_session_id() -> None:
    """Session placeholders must fall back to TOOL_CONTEXT.chat_session_id."""
    db = SessionLocal()
    try:
        proj = _make_project(db, "Ecisco BI")
        sess = _make_chat_session(db, str(proj.id))

        TOOL_CONTEXT.clear()
        TOOL_CONTEXT["chat_session_id"] = sess.id
        TOOL_CONTEXT["project_id"] = str(proj.id)

        result = _create_automation(
            {
                "name": "placeholder-session",
                "type": "custom",
                "schedule": "manual",
                "session_id": "TOOL_CONTEXT.chat_session_id",  # placeholder
            },
            db=db,
            user_id="u-test",
        )
        db.commit()

        row = db.get(AutomationTask, result["id"])
        assert row.session_id == sess.id
    finally:
        db.close()
