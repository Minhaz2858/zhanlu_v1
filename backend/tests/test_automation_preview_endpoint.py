"""Preview-token endpoints: mint + iframe-friendly token auth on preview."""
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

from app.database import SessionLocal
from app.deps import get_current_user_optional
from app.models.automation_execution import AutomationExecution
from app.models.automation_file import AutomationFile
from app.models.automation_task import AutomationTask
from app.models.user import User
from app.routers import automation_api
from app.services.preview_tokens import mint_preview_token


def _make_app(db):
    app = FastAPI()
    app.include_router(automation_api.router, prefix="/api")

    def _override_db():
        yield db

    app.dependency_overrides[automation_api.get_db] = _override_db
    return app


def _seed(db, tmp_path, org_id="org1"):
    user = User(
        id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@t.dev",
        full_name="T", role="user", password_hash="x", org_id=org_id,
    )
    task = AutomationTask(
        id=str(uuid.uuid4()), name="T", type="data_sync", org_id=org_id,
    )
    db.add_all([user, task])
    db.flush()
    execution = AutomationExecution(
        id=str(uuid.uuid4()), automation_task_id=task.id, status="completed",
        org_id=org_id,
    )
    db.add(execution)
    db.flush()
    f = tmp_path / "report.html"
    f.write_text("<html><body>hi</body></html>", encoding="utf-8")
    file_row = AutomationFile(
        id=str(uuid.uuid4()), execution_id=execution.id,
        automation_task_id=task.id, name="report.html", file_type="html",
        size=f.stat().st_size, file_path=str(f), mime_type="text/html",
        org_id=org_id,
    )
    db.add(file_row)
    db.commit()
    return user, file_row


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def test_mint_requires_auth(db, tmp_path):
    user, file_row = _seed(db, tmp_path)
    app = _make_app(db)
    app.dependency_overrides[get_current_user_optional] = lambda: None
    client = TestClient(app)
    resp = client.post(f"/api/automations/files/{file_row.id}/preview-token")
    assert resp.status_code == 401


def test_mint_returns_token_and_url(db, tmp_path):
    user, file_row = _seed(db, tmp_path)
    app = _make_app(db)
    app.dependency_overrides[get_current_user_optional] = lambda: user
    client = TestClient(app)
    resp = client.post(f"/api/automations/files/{file_row.id}/preview-token")
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]
    assert body["url"].endswith(f"/preview?token={body['token']}")


def test_preview_accepts_valid_token_without_bearer(db, tmp_path):
    user, file_row = _seed(db, tmp_path)
    app = _make_app(db)
    app.dependency_overrides[get_current_user_optional] = lambda: None
    client = TestClient(app)
    token = mint_preview_token(file_id=file_row.id, user_id=user.id)
    resp = client.get(f"/api/automations/files/{file_row.id}/preview?token={token}")
    assert resp.status_code == 200
    assert b"hi" in resp.content


def test_preview_rejects_bad_or_missing_token(db, tmp_path):
    user, file_row = _seed(db, tmp_path)
    app = _make_app(db)
    app.dependency_overrides[get_current_user_optional] = lambda: None
    client = TestClient(app)
    assert client.get(f"/api/automations/files/{file_row.id}/preview").status_code == 401
    assert client.get(
        f"/api/automations/files/{file_row.id}/preview?token=garbage.sig"
    ).status_code == 401
    other = mint_preview_token(file_id="different-file", user_id=user.id)
    assert client.get(
        f"/api/automations/files/{file_row.id}/preview?token={other}"
    ).status_code == 401


def test_preview_token_still_enforces_tenant(db, tmp_path):
    # Token is valid, but the minting user is in a different org than the file.
    user, file_row = _seed(db, tmp_path, org_id="org1")
    stranger = User(
        id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@t.dev",
        full_name="S", role="user", password_hash="x", org_id="org2",
    )
    db.add(stranger)
    db.commit()
    app = _make_app(db)
    app.dependency_overrides[get_current_user_optional] = lambda: None
    client = TestClient(app)
    token = mint_preview_token(file_id=file_row.id, user_id=stranger.id)
    resp = client.get(f"/api/automations/files/{file_row.id}/preview?token={token}")
    assert resp.status_code == 404  # _assert_tenant hides existence


def test_preview_bearer_path_unchanged(db, tmp_path):
    user, file_row = _seed(db, tmp_path)
    app = _make_app(db)
    app.dependency_overrides[get_current_user_optional] = lambda: user
    client = TestClient(app)
    resp = client.get(f"/api/automations/files/{file_row.id}/preview")
    assert resp.status_code == 200
