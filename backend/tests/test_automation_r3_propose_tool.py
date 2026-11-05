"""R3 — propose_automation tool: LLM writes ONLY drafts (TS §4.1)."""
from __future__ import annotations

import os
import sys
import uuid

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import Base  # noqa: E402
from app.models.automation_task import AutomationTask  # noqa: E402


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        from app.models.project import Project

        s.add(Project(id="prj-1", name="Propose Tool Project"))
        s.commit()
        yield s
    finally:
        s.close()
        engine.dispose()


def _user(db):
    from app.models.user import User

    u = User(
        id=f"u-{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:8]}@test.local",
        full_name="Proposer",
        password_hash="x",
        role="user",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _spec():
    import copy

    return copy.deepcopy({
        "name": "Proposed Sync",
        "project_id": "prj-1",
        "objective": "obj",
        "trigger": {"type": "schedule", "cron": "0 8 * * *", "timezone": "Asia/Shanghai"},
        "output": {"format": "html"},
        "delivery": {"chat": True, "email": False},
        "notifications": {"notify_on": "on_failure", "emails": []},
        "approvals": {"policy": "block", "pre_approved_scope": ["db.read"]},
        "retry": {"max_attempts": 2},
        "budgets": {"wall_clock_seconds": 600, "max_tool_calls": 60, "max_llm_calls": 40},
    })


def test_propose_creates_draft_not_definition(db):
    from app.services.automation_chat_tool import propose_automation_tool

    user = _user(db)
    out = propose_automation_tool({"spec": _spec()}, db, user.id)
    assert out["success"] is True
    assert out["draft_id"]
    assert out["validation"]["ok"] is True
    # The LLM cannot create a definition — only a draft exists.
    assert db.query(AutomationTask).count() == 0


def test_propose_surfaces_validation_errors(db):
    from app.services.automation_chat_tool import propose_automation_tool

    user = _user(db)
    spec = _spec()
    spec["trigger"]["cron"] = "not a cron !!!"
    out = propose_automation_tool({"spec": spec}, db, user.id)
    assert out["success"] is True
    assert out["validation"]["ok"] is False
    assert any(e["field"] == "trigger.cron" for e in out["validation"]["errors"])


def test_propose_requires_auth(db):
    from app.services.automation_chat_tool import propose_automation_tool

    out = propose_automation_tool({"spec": _spec()}, db, None)
    assert out["success"] is False


def test_propose_registered_in_registry():
    from app.services.tool_registry import registry

    assert "propose_automation" in registry.get_toolsets().get("automation", [])
