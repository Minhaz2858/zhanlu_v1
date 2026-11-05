"""R2 — AutomationSpec validation + automation_drafts foundation.

Covers roadmap R2:
- ``AutomationDraft`` model (§1.1): defaults, status lifecycle, spec JSON.
- New ``automation_tasks`` columns: ``spec``, ``spec_version``,
  ``confirmation_key`` (additive; legacy rows stay valid).
- Confirm idempotency arbiter: UNIQUE (org_id, confirmation_key)
  WHERE is_deleted = false — duplicates rejected per org, deleted rows
  free the key.
- ``services/automation/spec.py`` validation: table-driven valid/invalid
  cron / tz / budgets / scope / delivery cases.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

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
from app.models.automation_task import AutomationTask  # noqa: E402
from app.services.automation.spec import validate_spec  # noqa: E402


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


def _valid_spec() -> dict:
    return {
        "name": "Daily ERP Sales Sync Report",
        "project_id": "prj-1",
        "objective": "Synchronize ERP sales data and detect anomalies",
        "trigger": {
            "type": "schedule",
            "cron": "0 8 * * *",
            "timezone": "Asia/Shanghai",
            "misfire_policy": "fire_once_late",
            "grace_window_seconds": 3600,
        },
        "datasource_constraints": {"allowed_source_ids": [], "allow_workspace_auto_bind": True},
        "tool_constraints": {"allowed_tool_ids": [], "denied_tool_ids": []},
        "output": {"format": "html", "artifact_byte_cap": 10485760},
        "delivery": {"chat": True, "email": False},
        "notifications": {"notify_on": "on_failure", "emails": []},
        "approvals": {"policy": "block", "pre_approved_scope": ["db.read"], "approval_ttl_seconds": 900},
        "retry": {"max_attempts": 2, "backoff_base_seconds": 60, "backoff_cap_seconds": 1800},
        "budgets": {"wall_clock_seconds": 600, "max_tool_calls": 60, "max_llm_calls": 40},
    }


# ---------------------------------------------------------------------------
# Draft model
# ---------------------------------------------------------------------------


def test_draft_defaults_and_spec_roundtrip(db):
    spec = _valid_spec()
    d = AutomationDraft(spec=spec, origin_session_id=None)
    db.add(d)
    db.commit()
    db.refresh(d)
    assert d.id
    assert d.status == "draft"
    assert d.spec_version == 1
    assert d.spec["name"] == "Daily ERP Sales Sync Report"
    assert d.confirmed_definition_id is None
    assert d.confirmation_key is None


def test_draft_status_lifecycle(db):
    d = AutomationDraft(spec=_valid_spec())
    db.add(d)
    db.commit()
    d.status = "confirmed"
    d.confirmation_key = str(uuid.uuid4())
    d.confirmed_definition_id = "def-1"
    db.commit()
    db.refresh(d)
    assert d.status == "confirmed"
    d2 = AutomationDraft(spec=_valid_spec(), status="discarded")
    db.add(d2)
    db.commit()
    assert d2.status == "discarded"


def test_draft_spec_version_increments(db):
    d = AutomationDraft(spec=_valid_spec())
    db.add(d)
    db.commit()
    # JSON columns need reassignment (not in-place mutation) for change tracking.
    d.spec = {**d.spec, "name": "Renamed"}
    d.spec_version += 1
    db.commit()
    db.refresh(d)
    assert d.spec_version == 2
    assert d.spec["name"] == "Renamed"


# ---------------------------------------------------------------------------
# automation_tasks additive columns
# ---------------------------------------------------------------------------


def test_automation_task_spec_columns_additive(db):
    t = AutomationTask(name=f"R2 legacy {uuid.uuid4().hex[:6]}", type="report", prompt="p")
    db.add(t)
    db.commit()
    db.refresh(t)
    # Legacy row: spec is optional/NULL, version defaults to 1.
    assert t.spec is None
    assert t.spec_version == 1
    assert t.confirmation_key is None


# ---------------------------------------------------------------------------
# Confirm idempotency arbiter (partial unique index)
# ---------------------------------------------------------------------------


def _task_with_key(d, org, key, name):
    t = AutomationTask(
        name=name, type="report", prompt="p", org_id=org,
        confirmation_key=key, is_deleted=False,
    )
    d.add(t)
    d.commit()
    return t


def test_confirmation_key_unique_per_org(db):
    key = str(uuid.uuid4())
    _task_with_key(db, "org-a", key, "arbiter-1")
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        _task_with_key(db, "org-a", key, "arbiter-2")
    db.rollback()


def test_confirmation_key_unique_scoped_to_org(db):
    key = str(uuid.uuid4())
    _task_with_key(db, "org-a", key, "arbiter-orga")
    t2 = _task_with_key(db, "org-b", key, "arbiter-orgb")  # different org: OK
    assert t2.confirmation_key == key


def test_confirmation_key_freed_by_soft_delete(db):
    key = str(uuid.uuid4())
    t1 = _task_with_key(db, "org-a", key, "arbiter-del-1")
    t1.is_deleted = True
    db.commit()
    t2 = _task_with_key(db, "org-a", key, "arbiter-del-2")  # soft-deleted freed it
    assert t2.id


# ---------------------------------------------------------------------------
# Spec validation (table-driven)
# ---------------------------------------------------------------------------


def _errors(result):
    return {e["field"]: e["code"] for e in result["errors"]}


def test_validate_valid_spec():
    r = validate_spec(_valid_spec())
    assert r["ok"] is True, r


def test_validate_missing_name():
    spec = _valid_spec()
    spec.pop("name")
    r = validate_spec(spec)
    assert r["ok"] is False
    assert _errors(r).get("name") == "required"


def test_validate_name_too_long():
    spec = _valid_spec()
    spec["name"] = "x" * 256
    r = validate_spec(spec)
    assert _errors(r).get("name") == "too_long"


def test_validate_bad_cron():
    spec = _valid_spec()
    spec["trigger"]["cron"] = "not a cron at all !!!"
    r = validate_spec(spec)
    assert r["ok"] is False
    assert _errors(r).get("trigger.cron") == "unparseable"


def test_validate_bad_timezone():
    spec = _valid_spec()
    spec["trigger"]["timezone"] = "Mars/Olympus"
    r = validate_spec(spec)
    assert r["ok"] is False
    assert _errors(r).get("trigger.timezone") == "invalid_timezone"


def test_validate_once_requires_run_at():
    spec = _valid_spec()
    spec["trigger"] = {"type": "once", "timezone": "Asia/Shanghai"}
    r = validate_spec(spec)
    assert _errors(r).get("trigger.run_at") == "required"


def test_validate_once_run_at_must_be_future():
    spec = _valid_spec()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    spec["trigger"] = {"type": "once", "run_at": past, "timezone": "Asia/Shanghai"}
    r = validate_spec(spec)
    assert _errors(r).get("trigger.run_at") == "past"


def test_validate_bad_output_format():
    spec = _valid_spec()
    spec["output"]["format"] = "hologram"
    r = validate_spec(spec)
    assert _errors(r).get("output.format") == "invalid"


def test_validate_budget_over_ceiling():
    spec = _valid_spec()
    spec["budgets"]["wall_clock_seconds"] = 999999
    r = validate_spec(spec)
    assert _errors(r).get("budgets.wall_clock_seconds") == "over_ceiling"


def test_validate_bad_pre_approved_scope():
    spec = _valid_spec()
    spec["approvals"]["pre_approved_scope"] = ["db.read", "admin.destroy"]
    r = validate_spec(spec)
    assert _errors(r).get("approvals.pre_approved_scope") == "invalid_class"


def test_validate_bad_notify_on():
    spec = _valid_spec()
    spec["notifications"]["notify_on"] = "sometimes"
    r = validate_spec(spec)
    assert _errors(r).get("notifications.notify_on") == "invalid"


def test_validate_bad_email():
    spec = _valid_spec()
    spec["notifications"]["emails"] = ["not-an-email"]
    r = validate_spec(spec)
    assert _errors(r).get("notifications.emails") == "invalid"


def test_validate_unknown_trigger_type():
    spec = _valid_spec()
    spec["trigger"]["type"] = "telepathy"
    r = validate_spec(spec)
    assert _errors(r).get("trigger.type") == "invalid"


def test_validate_manual_trigger_has_no_cron_requirement():
    spec = _valid_spec()
    spec["trigger"] = {"type": "manual"}
    r = validate_spec(spec)
    assert r["ok"] is True, r


def test_validate_project_id_required():
    spec = _valid_spec()
    spec.pop("project_id")
    r = validate_spec(spec)
    assert _errors(r).get("project_id") == "required"


def test_validate_agent_existence_with_db(db):
    """agent_id must be null or reference an active agent app."""
    spec = _valid_spec()
    spec["agent"] = {"agent_id": "agt-does-not-exist"}
    r = validate_spec(spec, db=db)
    assert _errors(r).get("agent.agent_id") == "not_found"
