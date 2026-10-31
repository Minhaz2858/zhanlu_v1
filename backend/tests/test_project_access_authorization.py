"""Phase 0A — project-reference authorization (shared helper + entry points).

Covers: creator/admin/'use'-share authorized; unauthorized same-org user
404; cross-org 404; missing/deleted degrade; 'view' share insufficient;
system projects in-org runnable; and the conversation-creation endpoint
enforcing the same authorization as the message-resolution helper.
"""

import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.deps import get_current_user_required, get_db
from app.models.agent_conversation import AgentConversation
from app.models.project import Project
from app.models.resource_share import ResourceShare
from app.models.user import User
from app.services.project_access import (
    authorize_project_reference, project_use_authorized,
    resolve_authorized_message_project,
)

ORG = "org-acme"


def _user(uid, role="user", org=ORG):
    return User(id=uid, email=f"{uid}@t.dev", full_name=uid,
                role=role, password_hash="x", org_id=org)


def _project(pid, *, org=ORG, creator=None, is_system=False, name=None):
    return Project(id=pid, name=name or f"proj-{pid[:6]}", org_id=org,
                   created_by_id=creator, is_system=is_system)


class _Conv:
    """Minimal conversation stand-in for the resolver."""

    def __init__(self, project_id=None, org=ORG):
        self.project_id = project_id
        self.org_id = org


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[
        Project.__table__, ResourceShare.__table__, User.__table__,
        AgentConversation.__table__,
    ])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


def _seed(db, *, share_for=None, share_level="use"):
    pid = "p-victim-1"
    db.add_all([
        _user("u-owner", org=ORG),
        _user("u-admin", role="admin", org=ORG),
        _user("u-other", org=ORG),
        _user("u-foreign", role="admin", org="org-evil"),
        _project(pid, org=ORG, creator="u-owner"),
        _project("p-sys", org=ORG, is_system=True),
        _project("p-evil", org="org-evil", creator="u-foreign"),
    ])
    if share_for:
        db.add(ResourceShare(
            resource_type="project", resource_id=pid,
            shared_with_user_id=share_for, access_level=share_level,
            org_id=ORG, created_by_id="u-owner",
        ))
    db.commit()
    return pid


def test_creator_can_use_their_project(db):
    pid = _seed(db)
    p = authorize_project_reference(db, _user("u-owner"), pid)
    assert p is not None and p.id == pid


def test_org_admin_can_use_any_project_in_their_org(db):
    pid = _seed(db)
    p = authorize_project_reference(db, _user("u-admin", role="admin"), pid)
    assert p is not None and p.id == pid


def test_unauthorized_same_org_user_gets_404(db):
    pid = _seed(db)
    with pytest.raises(HTTPException) as ei:
        authorize_project_reference(db, _user("u-other"), pid)
    assert ei.value.status_code == 404


def test_use_share_grants_access_but_view_share_does_not(db):
    pid = _seed(db, share_for="u-other", share_level="use")
    assert authorize_project_reference(db, _user("u-other"), pid) is not None
    # same db, downgrade the grant to 'view'
    share = db.query(ResourceShare).filter(
        ResourceShare.shared_with_user_id == "u-other").one()
    share.access_level = "view"
    db.commit()
    with pytest.raises(HTTPException):
        authorize_project_reference(db, _user("u-other"), pid)


def test_cross_org_access_rejected_even_for_foreign_admin(db):
    _seed(db)
    with pytest.raises(HTTPException) as ei:
        authorize_project_reference(db, _user("u-foreign", role="admin",
                                              org="org-evil"), "p-victim-1")
    assert ei.value.status_code == 404


def test_missing_and_deleted_projects_degrade_to_none(db):
    _seed(db)
    assert authorize_project_reference(db, _user("u-owner"), "nope") is None
    victim = db.query(Project).filter(Project.id == "p-victim-1").one()
    victim.is_deleted = True
    db.commit()
    assert authorize_project_reference(db, _user("u-owner"),
                                       "p-victim-1") is None


def test_system_project_runnable_in_org_but_not_cross_org(db):
    _seed(db)
    assert project_use_authorized(db, _user("u-other"),
                                  authorize_project_reference(
                                      db, _user("u-other"), "p-sys"))
    with pytest.raises(HTTPException):
        authorize_project_reference(db, _user("u-foreign", role="admin",
                                              org="org-evil"), "p-sys")


def test_message_resolver_enforces_same_authorization(db):
    """Both chat entry points call this resolver — the 404 wall applies
    identically to body project_id and body project_name."""
    _seed(db)
    conv = _Conv(project_id="p-victim-1")  # conv created by owner earlier
    owner = _user("u-owner")
    assert resolve_authorized_message_project(
        db, user=owner, conv=conv,
        body_project_id="p-victim-1") == "p-victim-1"
    with pytest.raises(HTTPException):
        resolve_authorized_message_project(
            db, user=_user("u-other"), conv=conv,
            body_project_id="p-victim-1")
    with pytest.raises(HTTPException):
        resolve_authorized_message_project(
            db, user=_user("u-other"), conv=conv,
            body_project_name=conv and db.query(Project).filter(
                Project.id == "p-victim-1").one().name)
    # stale body id falls back to the authorized conv project (owner's)
    assert resolve_authorized_message_project(
        db, user=owner, conv=conv, body_project_id="ghost") == "p-victim-1"


def test_conversation_creation_endpoint_enforces_authorization(db):
    """The creation entry point must 404 on an unauthorized project and
    drop stale ids — same helper as the message resolver."""
    from app.routers.agents import router as agents_router

    _seed(db)

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(agents_router, prefix="/api")

    def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user_required] = lambda: _user("u-other")
    client = TestClient(app, raise_server_exceptions=False)

    # unauthorized existing project → 404
    r = client.post("/api/apps/default-app/agents/conversations",
                    json={"agent_name": "general_assistant",
                          "project_id": "p-victim-1"})
    assert r.status_code == 404

    # authorized creator → created with the project
    client2 = TestClient(app)
    app.dependency_overrides[get_current_user_required] = lambda: _user("u-owner")
    r2 = client2.post("/api/apps/default-app/agents/conversations",
                      json={"agent_name": "general_assistant",
                            "metadata": {"project_id": "p-victim-1"}})
    assert r2.status_code == 200
    assert r2.json().get("project_id") == "p-victim-1"

    # stale project id → dropped gracefully, conversation still created
    r3 = client2.post("/api/apps/default-app/agents/conversations",
                      json={"agent_name": "general_assistant",
                            "metadata": {"project_id": "ghost"}})
    assert r3.status_code == 200
    assert r3.json().get("project_id") in (None, "")
