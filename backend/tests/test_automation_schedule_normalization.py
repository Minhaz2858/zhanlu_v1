"""Regression tests for the automation schedule-normalization fixes.

Guards the silent-never-fires class of bugs found in the 2026-09-02 audit:

1. Generic-entity CRUD (base44.entities.AutomationTask.create/update) never
   parsed ``schedule`` → cron_expression/next_run_at stayed empty and the
   dispatcher (fires ONLY status==\"active\" with next_run_at) skipped the
   task silently. The entities router now normalizes every create/update via
   ``_normalize_automation_schedule_payload`` (mirrors the chat tool).

2. Resuming (POST /{task_id}/toggle → active) only recomputed next_run_at when
   cron_expression was already truthy — a task with a cron-looking schedule
   but empty cron_expression resumed to a dead state. It now recomputes (and
   fails loudly with 400) whenever a schedule exists but can't be parsed.

3. One-shot datetime schedules (\"2026-08-15 14:30\") are rejected instead of
   being LLM-fabricated into a yearly cron.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.deps import get_current_user_optional
from app.models.automation_task import AutomationTask
from app.routers import entities as entities_router
from app.routers import automation_api


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


_FAKE_USER = SimpleNamespace(id="user-1", org_id=None, role="user")


# --- Helper-level tests ------------------------------------------------------

def test_normalize_payload_create_cron_and_active_default():
    from app.routers.entities import _normalize_automation_schedule_payload
    data = {"name": "t", "type": "scheduled", "schedule": "0 9 * * *"}
    _normalize_automation_schedule_payload(data, creating=True)
    assert data["cron_expression"] == "0 9 * * *"
    assert data["status"] == "active"
    assert data["next_run_at"]
    assert data["timezone"] == "UTC"


def test_normalize_payload_strips_custom_label():
    from app.routers.entities import _normalize_automation_schedule_payload
    data = {"name": "t", "type": "scheduled", "schedule": "Custom: 0 8 * * *"}
    _normalize_automation_schedule_payload(data, creating=True)
    assert data["cron_expression"] == "0 8 * * *"


def test_normalize_payload_rejects_once_datetime():
    from app.routers.entities import _normalize_automation_schedule_payload
    with pytest.raises(HTTPException) as ei:
        _normalize_automation_schedule_payload(
            {"name": "t", "schedule": "2026-08-15 14:30"}, creating=True
        )
    assert ei.value.status_code == 400


def test_normalize_payload_update_skips_unchanged_schedule():
    """Editing name/description must NOT rewrite cron/next_run of a healthy task."""
    from app.routers.entities import _normalize_automation_schedule_payload
    data = {"name": "renamed", "schedule": "daily at 09:00"}
    current = {"schedule": "daily at 09:00", "cron_expression": "0 9 * * *", "timezone": "UTC"}
    _normalize_automation_schedule_payload(data, current=current, creating=False)
    assert "cron_expression" not in data
    assert "next_run_at" not in data


def test_normalize_payload_repairs_dead_task_on_update():
    """Same schedule + empty cron_expression = dead-task repair path."""
    from app.routers.entities import _normalize_automation_schedule_payload
    data = {"name": "repair", "schedule": "0 9 * * *"}
    current = {"schedule": "0 9 * * *", "cron_expression": "", "timezone": "UTC"}
    _normalize_automation_schedule_payload(data, current=current, creating=False)
    assert data["cron_expression"] == "0 9 * * *"
    assert data["next_run_at"]


# --- HTTP-level guard: generic entity create parses schedules ---------------

def _entity_app(db):
    app = FastAPI()
    app.include_router(
        entities_router.register_entity_router("AutomationTask", AutomationTask),
        prefix="/api",
    )

    def _override_db():
        yield db

    app.dependency_overrides[entities_router.get_db] = _override_db
    app.dependency_overrides[get_current_user_optional] = lambda: _FAKE_USER
    return TestClient(app)


def test_entity_create_parses_schedule_into_cron_and_next_run(db):
    client = _entity_app(db)
    resp = client.post(
        "/api/apps/demo/entities/AutomationTask",
        json={"name": "Sales", "type": "scheduled", "schedule": "0 9 * * *"},
    )
    assert resp.status_code == 200, resp.text
    row = db.query(AutomationTask).filter(AutomationTask.name == "Sales").first()
    assert row is not None
    assert row.cron_expression == "0 9 * * *"
    assert row.status == "active"  # tool parity: valid cron => active default
    assert row.next_run_at is not None
    assert row.timezone == "UTC"


def test_entity_create_rejects_unparseable_schedule(db):
    client = _entity_app(db)
    resp = client.post(
        "/api/apps/demo/entities/AutomationTask",
        json={"name": "Bad", "type": "scheduled", "schedule": "2026-08-15 14:30"},
    )
    assert resp.status_code == 400
    # Nothing persisted — a dead task must never be created through the API.
    assert db.query(AutomationTask).filter(AutomationTask.name == "Bad").first() is None


def test_entity_update_schedule_reparses(db):
    client = _entity_app(db)
    created = client.post(
        "/api/apps/demo/entities/AutomationTask",
        json={"name": "Editable", "type": "scheduled", "schedule": "0 9 * * *"},
    ).json()
    task_id = created["id"]
    resp = client.put(
        f"/api/apps/demo/entities/AutomationTask/{task_id}",
        json={"name": "Editable", "type": "scheduled", "schedule": "0 7 * * *", "timezone": "UTC"},
    )
    assert resp.status_code == 200, resp.text
    db.expire_all()
    row = db.get(AutomationTask, task_id)
    assert row.cron_expression == "0 7 * * *"
    assert row.next_run_at is not None


# --- HTTP-level guard: toggle resumes dead tasks / fails loudly --------------

def _toggle_app(db):
    app = FastAPI()
    app.include_router(automation_api.router, prefix="/api")

    def _override_db():
        yield db

    app.dependency_overrides[automation_api.get_db] = _override_db
    app.dependency_overrides[get_current_user_optional] = lambda: _FAKE_USER
    return TestClient(app)


def _make_task(db, **overrides):
    task = AutomationTask(
        id=str(uuid.uuid4()),
        name="ResumeMe",
        type="scheduled",
        schedule="0 9 * * *",
        status="paused",
        **overrides,
    )
    db.add(task)
    db.commit()
    return task


def test_toggle_resume_repairs_empty_cron_expression(db, monkeypatch):
    """A paused task with cron-looking schedule but empty cron_expression must
    recompute next_run_at on resume (previously resumed dead: active + NULL)."""
    task = _make_task(db)  # cron_expression intentionally None
    recomputed = datetime.now(timezone.utc) + timedelta(hours=1)
    monkeypatch.setattr(
        "app.services.automation_dispatcher.recompute_next_run",
        lambda task_id: recomputed,
    )
    client = _toggle_app(db)
    resp = client.post(f"/api/automations/{task.id}/toggle", json={"status": "active"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "active"
    assert body["next_run_at"] is not None


def test_toggle_resume_fails_loudly_when_schedule_unparseable(db, monkeypatch):
    task = _make_task(db, schedule="2026-08-15 14:30")
    monkeypatch.setattr(
        "app.services.automation_dispatcher.recompute_next_run",
        lambda task_id: None,
    )
    client = _toggle_app(db)
    resp = client.post(f"/api/automations/{task.id}/toggle", json={"status": "active"})
    assert resp.status_code == 400  # never silently resume to the dead state


def test_toggle_pause_is_noop_for_next_run(db, monkeypatch):
    task = _make_task(db, cron_expression="0 9 * * *")

    def _fail_if_recompute_called(*args, **kwargs):
        raise AssertionError("pausing must never recompute next_run_at")

    monkeypatch.setattr(
        "app.services.automation_dispatcher.recompute_next_run",
        _fail_if_recompute_called,
    )
    client = _toggle_app(db)
    resp = client.post(f"/api/automations/{task.id}/toggle", json={"status": "paused"})
    assert resp.status_code == 200
