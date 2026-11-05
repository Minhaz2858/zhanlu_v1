"""Lifecycle separation — CREATE DRAFT ≠ CONFIRM ≠ EXECUTE ≠ ARTIFACT.

Regression tests for the automation-creation bug: chat creation used the
legacy ``create_automation`` CRUD tool (direct ACTIVE task insert, implicit
single-KB datasource pick), the file-turn guard turned "output format HTML"
into a forced ``create_artifact`` in the creation turn, and the agent could
run the just-created task via ``execute_automation`` in the same turn.

Cutover flag: ``AUTOMATION_CREATION_V2`` (default ON).
"""
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
from app.models.automation_draft import AutomationDraft  # noqa: E402
from app.models.automation_execution import AutomationExecution  # noqa: E402
from app.models.automation_scheduling import (  # noqa: E402
    AutomationOccurrence,
)
from app.models.automation_task import AutomationTask  # noqa: E402
from app.models.user import User  # noqa: E402


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture
def project(db):
    from app.models.project import Project
    p = Project(name=f"Prj {uuid.uuid4().hex[:6]}")
    db.add(p)
    db.commit()
    return p


@pytest.fixture
def user(db):
    u = User(id=f"u-{uuid.uuid4().hex[:8]}", email=f"t{uuid.uuid4().hex[:6]}@x.io",
             full_name="Tester", password_hash="x")
    db.add(u)
    db.commit()
    return u


# ---------------------------------------------------------------------------
# Toolset cutover (RC1/RC2/RC4)
# ---------------------------------------------------------------------------

def test_automation_agent_toolset_has_propose_not_create():
    """The chat-visible toolset for automation_agent exposes the draft path
    and NOT the legacy active-creating tools."""
    from app.services.agent_prompts import AUTOMATION_AGENT_TOOLS

    names = {t["function"]["name"] for t in AUTOMATION_AGENT_TOOLS}
    assert "propose_automation" in names
    assert "update_automation_draft" in names
    assert "create_automation" not in names      # legacy: gone
    assert "execute_automation" not in names     # no same-turn runs (RC4)
    # RC2: no silent datasource pick instruction anywhere in the toolset
    blob = "".join(str(t) for t in AUTOMATION_AGENT_TOOLS)
    assert "without asking the user" not in blob
    assert "use it directly without asking" not in blob


def test_tool_registry_defaults_match_cutover():
    from app.services.tool_registry import DEFAULT_TOOLS_BY_AGENT

    tools = DEFAULT_TOOLS_BY_AGENT.get("automation_agent") or []
    assert "propose_automation" in tools
    assert "update_automation_draft" in tools
    assert "create_automation" not in tools
    assert "execute_automation" not in tools


def test_automation_prompt_teaches_draft_path():
    from app.services.agent_prompts import AUTOMATION_AGENT_SYSTEM_PROMPT

    assert "propose_automation" in AUTOMATION_AGENT_SYSTEM_PROMPT
    assert "call `create_automation`" not in AUTOMATION_AGENT_SYSTEM_PROMPT
    # configuration-phase contract is stated
    assert "CONFIGURATION PHASE" in AUTOMATION_AGENT_SYSTEM_PROMPT.upper()


# ---------------------------------------------------------------------------
# Legacy handler guards (RC1/RC6 — deterministic, not prompt-level)
# ---------------------------------------------------------------------------

_VALID_ARGS = {
    "name": "Daily Sales Data Sync",
    "type": "data_sync",
    "description": "sync",
    "schedule": "0 8 * * *",
}


def test_create_automation_refuses_under_v2(db, user, monkeypatch):
    """The legacy CRUD handler must not create ANY task row under the V2
    cutover — even if some toolset still exposes it (defense in depth)."""
    from app.config import settings
    from app.services import agent_tools

    monkeypatch.setattr(settings, "AUTOMATION_CREATION_V2", True)
    result = agent_tools._create_automation(
        dict(_VALID_ARGS), db, user.id)
    assert result.get("success") is False
    assert "propose_automation" in str(result.get("error", "")).lower() or \
        "draft" in str(result.get("error", "")).lower()
    assert db.query(AutomationTask).filter(
        AutomationTask.name == "Daily Sales Data Sync").count() == 0


def test_create_automation_legacy_still_works_flag_off(db, user, monkeypatch):
    """Rollback path: flag OFF restores the legacy behavior."""
    from app.config import settings
    from app.services import agent_tools

    monkeypatch.setattr(settings, "AUTOMATION_CREATION_V2", False)
    result = agent_tools._create_automation(
        dict(_VALID_ARGS), db, user.id)
    assert result.get("success") is True
    assert db.query(AutomationTask).filter(
        AutomationTask.name == "Daily Sales Data Sync").count() == 1


def test_update_automation_cannot_activate_under_v2(db, user, monkeypatch):
    from app.config import settings
    from app.services import agent_tools

    monkeypatch.setattr(settings, "AUTOMATION_CREATION_V2", True)
    t = AutomationTask(name=f"LGC {uuid.uuid4().hex[:6]}", type="report",
                       prompt="p", status="paused")
    db.add(t)
    db.commit()
    result = agent_tools._update_automation(
        {"automation_id": t.id, "fields": {"status": "active"}},
        db, user.id)
    db.expire_all()
    assert db.get(AutomationTask, t.id).status == "paused"
    assert result.get("success") is False or \
        "activat" in str(result.get("error", "")).lower()


# ---------------------------------------------------------------------------
# Draft path (RC1) + update_automation_draft tool
# ---------------------------------------------------------------------------

_SPEC = {
    "name": "Daily Sales Data Sync",
    "trigger": {"type": "schedule", "cron": "0 8 * * *", "timezone": "UTC"},
    "objective": "Sync ERP sales data",
}


def test_propose_automation_writes_draft_no_task(db, user, project):
    from app.services.automation_chat_tool import propose_automation_tool

    result = propose_automation_tool(
        {"spec": dict(_SPEC, project_id=project.id)}, db, user.id, context={})
    assert result["success"] is True
    assert db.query(AutomationDraft).filter(
        AutomationDraft.id == result["draft_id"]).count() == 1
    # NO task, NO execution, NO occurrence
    assert db.query(AutomationTask).filter(
        AutomationTask.name == "Daily Sales Data Sync").count() == 0
    assert db.query(AutomationExecution).count() == 0
    assert db.query(AutomationOccurrence).count() == 0


def test_propose_automation_without_datasource_flags_warning(db, user, project):
    """RC2: a missing datasource is a VALIDATION WARNING surfaced to the
    user — never a silent auto-pick."""
    from app.services.automation_chat_tool import propose_automation_tool

    result = propose_automation_tool(
        {"spec": dict(_SPEC, project_id=project.id)}, db, user.id, context={})
    validation = result.get("validation") or {}
    warnings = [str(w) for w in (validation.get("warnings") or [])]
    assert any("datasource" in w.lower() or "data source" in w.lower()
               for w in warnings)


def test_update_automation_draft_edits_draft_only(db, user, project):
    from app.services.automation_chat_tool import (
        propose_automation_tool, update_automation_draft_tool,
    )

    proposed = propose_automation_tool(
        {"spec": dict(_SPEC, project_id=project.id)}, db, user.id, context={})
    draft_id = proposed["draft_id"]

    result = update_automation_draft_tool(
        {"draft_id": draft_id, "spec_patch": {"description": "updated desc"}},
        db, user.id)
    assert result.get("success") is True
    draft = db.get(AutomationDraft, draft_id)
    assert draft.spec.get("description") == "updated desc"
    # still no task/execution
    assert db.query(AutomationTask).filter(
        AutomationTask.name == "Daily Sales Data Sync").count() == 0
    assert db.query(AutomationExecution).count() == 0


# ---------------------------------------------------------------------------
# Confirmation semantics (activate inside the transaction; no side effects)
# ---------------------------------------------------------------------------

def _confirmed(db, user, activate, project):
    from app.services.automation.control_plane import confirm_draft, create_draft

    draft = create_draft(db, user, dict(_SPEC, project_id=project.id))
    definition, response = confirm_draft(
        db, user, draft.id, f"ck-{uuid.uuid4().hex}",
        if_match=draft.spec_version, activate=activate)
    return definition, response


def test_confirm_activate_creates_active_definition(db, user, project):
    definition, _ = _confirmed(db, user, activate=True, project=project)
    assert definition.status == "active"
    assert definition.cron_expression == "0 8 * * *"
    # NO execution side effects at confirmation
    assert db.query(AutomationExecution).filter(
        AutomationExecution.automation_task_id == definition.id).count() == 0
    assert db.query(AutomationOccurrence).filter(
        AutomationOccurrence.automation_task_id == definition.id).count() == 0


def test_confirm_default_stays_paused(db, user, project):
    definition, _ = _confirmed(db, user, activate=False, project=project)
    assert definition.status == "paused"


def test_confirm_idempotent_replay_no_duplicate(db, user, project):
    from app.services.automation.control_plane import confirm_draft, create_draft

    draft = create_draft(db, user, dict(_SPEC, project_id=project.id))
    key = f"ck-{uuid.uuid4().hex}"
    d1, r1 = confirm_draft(db, user, draft.id, key,
                           if_match=draft.spec_version, activate=True)
    d2, r2 = confirm_draft(db, user, draft.id, key,
                           if_match=draft.spec_version, activate=True)
    assert d1.id == d2.id
    assert db.query(AutomationTask).filter(
        AutomationTask.confirmation_key == key).count() == 1
