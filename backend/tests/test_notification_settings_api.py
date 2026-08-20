"""Email Notification Gateway API: notification-settings PATCH + download route.

Covers:
- PATCH /api/automations/{task_id}/notification-settings persists + validates
  (422 on bad email / unknown notify_on, 404 on missing task)
- GET /api/automations/email-download/{file_id}?token= serves the file for a
  valid HMAC token and rejects missing/garbage/mis-bound tokens
"""
import os
import sys
import uuid

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.database import SessionLocal, engine
from app.models.automation_execution import AutomationExecution
from app.models.automation_file import AutomationFile
from app.models.automation_task import AutomationTask
from app.models.base import Base
from app.routers import automation_api
from app.services.notification_gateway import generate_download_token


@pytest.fixture(autouse=True)
def _clean_slate():
    """Fresh schema + empty automation rows for every test.

    The shared in-memory SQLite database persists across the whole session
    (StaticPool), so tables must be created once and rows cleaned per test.
    ``app.database`` compiles PostgreSQL JSONB to plain JSON on SQLite so the
    full ``Base.metadata`` is safe to create here.
    """
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        db.execute(delete(AutomationFile))
        db.execute(delete(AutomationExecution))
        db.execute(delete(AutomationTask))
        db.commit()
    finally:
        db.close()
    yield


def _make_app(db):
    app = FastAPI()
    app.include_router(automation_api.router, prefix="/api")

    def _override_db():
        yield db

    app.dependency_overrides[automation_api.get_db] = _override_db
    return app


def _seed_task(db, org_id="org1"):
    task = AutomationTask(
        id=str(uuid.uuid4()), name="Email Task", type="data_sync", org_id=org_id,
        notify_enabled=False, notify_emails=[], notify_on="always", attach_file=True,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _seed_file(db, tmp_path, task, org_id="org1"):
    execution = AutomationExecution(
        id=str(uuid.uuid4()), automation_task_id=task.id, status="completed",
    )
    db.add(execution)
    db.flush()
    f = tmp_path / "report.html"
    f.write_text("<html><body>result!</body></html>", encoding="utf-8")
    file_row = AutomationFile(
        id=str(uuid.uuid4()), execution_id=execution.id,
        automation_task_id=task.id, name="report.html",
        file_type="html", size=f.stat().st_size, file_path=str(f),
        mime_type="text/html", org_id=org_id,
    )
    db.add(file_row)
    db.commit()
    db.refresh(file_row)
    return file_row


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ── notification-settings PATCH ─────────────────────────────────────────────
def test_update_persists_and_returns_settings(db):
    task = _seed_task(db)
    app = _make_app(db)
    client = TestClient(app)

    resp = client.patch(
        f"/api/automations/{task.id}/notification-settings",
        json={
            "notify_enabled": True,
            "notify_emails": ["Boss@Example.com", "ops@example.com"],
            "notify_on": "on_failure",
            "attach_file": False,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["notify_enabled"] is True
    # Emails are normalised to lower case server-side.
    assert body["notify_emails"] == ["boss@example.com", "ops@example.com"]
    assert body["notify_on"] == "on_failure"
    assert body["attach_file"] is False

    db.expire_all()
    stored = db.query(AutomationTask).filter(AutomationTask.id == task.id).first()
    assert stored.notify_enabled is True
    assert stored.notify_emails == ["boss@example.com", "ops@example.com"]
    assert stored.notify_on == "on_failure"
    assert stored.attach_file is False


def test_update_rejects_invalid_email(db):
    task = _seed_task(db)
    app = _make_app(db)
    client = TestClient(app)

    resp = client.patch(
        f"/api/automations/{task.id}/notification-settings",
        json={
            "notify_enabled": True,
            "notify_emails": ["boss@example.com", "not-an-email"],
            "notify_on": "always",
            "attach_file": True,
        },
    )
    assert resp.status_code == 422
    assert "not-an-email" in resp.json()["detail"]


def test_update_rejects_unknown_notify_on(db):
    task = _seed_task(db)
    app = _make_app(db)
    client = TestClient(app)

    resp = client.patch(
        f"/api/automations/{task.id}/notification-settings",
        json={
            "notify_enabled": True,
            "notify_emails": ["boss@example.com"],
            "notify_on": "every_blue_moon",
            "attach_file": True,
        },
    )
    assert resp.status_code == 422


def test_update_404_when_task_missing(db):
    app = _make_app(db)
    client = TestClient(app)
    resp = client.patch(
        "/api/automations/does-not-exist/notification-settings",
        json={
            "notify_enabled": True,
            "notify_emails": ["boss@example.com"],
            "notify_on": "always",
            "attach_file": True,
        },
    )
    assert resp.status_code == 404


# ── email download route ────────────────────────────────────────────────────
def test_email_download_serves_with_valid_token(db, tmp_path):
    task = _seed_task(db)
    file_row = _seed_file(db, tmp_path, task)
    app = _make_app(db)
    client = TestClient(app)

    token = generate_download_token(file_row.id)
    resp = client.get(f"/api/automations/email-download/{file_row.id}?token={token}")
    assert resp.status_code == 200
    assert b"result!" in resp.content


def test_email_download_rejects_missing_or_bad_token(db, tmp_path):
    task = _seed_task(db)
    file_row = _seed_file(db, tmp_path, task)
    app = _make_app(db)
    client = TestClient(app)

    assert client.get(
        f"/api/automations/email-download/{file_row.id}"
    ).status_code == 401
    assert client.get(
        f"/api/automations/email-download/{file_row.id}?token=garbage.sig"
    ).status_code == 401


def test_email_download_token_is_bound_to_file(db, tmp_path):
    task = _seed_task(db)
    file_a = _seed_file(db, tmp_path, task)
    other = file_a.id + "-different"
    app = _make_app(db)
    client = TestClient(app)

    token = generate_download_token(other)
    resp = client.get(f"/api/automations/email-download/{file_a.id}?token={token}")
    assert resp.status_code == 401


def test_email_download_404_when_file_missing(db, tmp_path):
    task = _seed_task(db)
    app = _make_app(db)
    client = TestClient(app)

    token = generate_download_token("no-such-file")
    resp = client.get(f"/api/automations/email-download/no-such-file?token={token}")
    assert resp.status_code == 404
