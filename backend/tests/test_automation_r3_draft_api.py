"""R3 — Control Plane API routes: draft CRUD + confirm over HTTP-shaped calls.

Thin route-layer checks (the §4.2 transaction itself is covered exhaustively
in test_automation_r3_confirm.py): status-code mapping of ControlPlaneError
codes, If-Match version flow, and the 201/200/409 confirm contract.
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
from fastapi import HTTPException  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import Base  # noqa: E402


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        from app.models.project import Project

        s.add(Project(id="prj-1", name="R3 API Project"))
        s.commit()
        yield s
    finally:
        s.close()
        engine.dispose()


def _user(org="default-org", role="user"):
    from types import SimpleNamespace

    return SimpleNamespace(id=f"u-{uuid.uuid4().hex[:6]}", org_id=org, role=role)


def _spec():
    return {
        "name": f"API Draft {uuid.uuid4().hex[:6]}",
        "project_id": "prj-1",
        "objective": "obj",
        "trigger": {"type": "schedule", "cron": "0 8 * * *", "timezone": "Asia/Shanghai"},
        "output": {"format": "html"},
        "delivery": {"chat": True, "email": False},
        "notifications": {"notify_on": "on_failure", "emails": []},
        "approvals": {"policy": "block", "pre_approved_scope": ["db.read"]},
        "retry": {"max_attempts": 2},
        "budgets": {"wall_clock_seconds": 600, "max_tool_calls": 60, "max_llm_calls": 40},
    }


def _routes():
    from app.routers import automation_api

    return automation_api


def test_draft_route_lifecycle(db):
    api = _routes()
    user = _user()
    created = api.create_automation_draft(
        body=api._DraftSpecBody(spec_partial=_spec()), db=db, user=user
    )
    assert created["version"] == 1
    assert created["validation"]["ok"] is True

    got = api.get_automation_draft(draft_id=created["draft_id"], db=db, user=user)
    assert got["draft_id"] == created["draft_id"]

    patched = api.patch_automation_draft(
        draft_id=created["draft_id"],
        body=api._DraftSpecBody(spec_partial={"name": "Renamed"}),
        if_match=1, db=db, user=user,
    )
    assert patched["version"] == 2
    assert patched["spec"]["name"] == "Renamed"

    key = str(uuid.uuid4())
    confirmed = api.confirm_automation_draft(
        draft_id=created["draft_id"],
        body=api._ConfirmBody(confirmation_key=key),
        if_match=2, db=db, user=user,
    )
    assert confirmed["already_confirmed"] is False
    assert confirmed["definition"]["name"] == "Renamed"

    replay = api.confirm_automation_draft(
        draft_id=created["draft_id"],
        body=api._ConfirmBody(confirmation_key=key),
        if_match=2, db=db, user=user,
    )
    assert replay["already_confirmed"] is True
    assert replay["definition_id"] == confirmed["definition_id"]


def test_confirm_stale_version_maps_409(db):
    api = _routes()
    user = _user()
    created = api.create_automation_draft(
        body=api._DraftSpecBody(spec_partial=_spec()), db=db, user=user
    )
    with pytest.raises(HTTPException) as exc:
        api.confirm_automation_draft(
            draft_id=created["draft_id"],
            body=api._ConfirmBody(confirmation_key=str(uuid.uuid4())),
            if_match=99, db=db, user=user,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "stale_version"
    assert exc.value.detail["current_version"] == 1


def test_get_unknown_draft_maps_404(db):
    api = _routes()
    with pytest.raises(HTTPException) as exc:
        api.get_automation_draft(draft_id="nope", db=db, user=_user())
    assert exc.value.status_code == 404


def test_discard_route(db):
    api = _routes()
    user = _user()
    created = api.create_automation_draft(
        body=api._DraftSpecBody(spec_partial=_spec()), db=db, user=user
    )
    resp = api.discard_automation_draft(draft_id=created["draft_id"], db=db, user=user)
    assert resp["status"] == "discarded"
    with pytest.raises(HTTPException) as exc:
        api.confirm_automation_draft(
            draft_id=created["draft_id"],
            body=api._ConfirmBody(confirmation_key=str(uuid.uuid4())),
            if_match=1, db=db, user=user,
        )
    assert exc.value.detail["code"] == "draft_discarded"
