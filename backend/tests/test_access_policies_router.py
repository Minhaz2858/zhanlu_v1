"""Router tests for /api/access-policies CRUD + authorization."""

import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal, get_db
from app.deps import get_current_user_required
from app.models.project import Project
from app.models.knowledge_base import KnowledgeBase
from app.models.resource_share import ResourceShare
from app.models.resource_access_policy import ResourceAccessPolicy
from app.models.user import User
from app.routers.access_policies import router as ap_router
import app.models  # noqa: F401  register all models


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _seed_user(db, role="user"):
    u = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@t.io",
             full_name="t", role=role, password_hash="x",
             org_id="default-org", app_id="default-app")
    db.add(u)
    db.commit()
    return u


def _seed_project(db, owner_id):
    p = Project(name=f"p{uuid.uuid4().hex[:6]}", org_id="default-org",
                app_id="default-app", created_by_id=owner_id)
    db.add(p)
    db.commit()
    return p


def _seed_kb(db, project_id=None, name="kb"):
    kb = KnowledgeBase(name=name, source_kind="database", db_type="sqlite",
                       org_id="default-org", app_id="default-app",
                       project_id=project_id)
    db.add(kb)
    db.commit()
    return kb


def _seed_share(db, owner_id, recipient_id, resource_type="project", resource_id=None):
    s = ResourceShare(resource_type=resource_type,
                      resource_id=resource_id or str(uuid.uuid4()),
                      shared_with_user_id=recipient_id,
                      access_level="use", created_by_id=owner_id)
    db.add(s)
    db.commit()
    return s


def _client(db, user):
    app = FastAPI()
    app.include_router(ap_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: user
    return TestClient(app)


def test_list_policies_empty(db):
    owner = _seed_user(db)
    recipient = _seed_user(db)
    proj = _seed_project(db, owner.id)
    _seed_share(db, owner.id, recipient.id, resource_id=proj.id)

    c = _client(db, owner)
    r = c.get("/api/access-policies",
              params={"resource_type": "project", "resource_id": proj.id,
                      "user_id": recipient.id})
    assert r.status_code == 200, r.text
    assert r.json()["policies"] == []


def test_list_policies_404_when_no_share(db):
    owner = _seed_user(db)
    c = _client(db, owner)
    r = c.get("/api/access-policies",
              params={"resource_type": "project", "resource_id": "nope",
                      "user_id": "nobody"})
    assert r.status_code == 404


def test_upsert_policies_by_owner(db):
    owner = _seed_user(db)
    recipient = _seed_user(db)
    proj = _seed_project(db, owner.id)
    _seed_share(db, owner.id, recipient.id, resource_id=proj.id)

    c = _client(db, owner)
    body = {
        "resource_type": "project",
        "resource_id": proj.id,
        "user_id": recipient.id,
        "policies": [
            {"kb_id": "kb1", "table_name": "Secrets", "mode": "deny"},
            {"kb_id": "kb2", "table_name": None, "mode": "deny"},
        ],
    }
    r = c.put("/api/access-policies", json=body)
    assert r.status_code == 200, r.text
    data = r.json()["policies"]
    assert len(data) == 2
    # table_name is lowercased on save.
    by_kb = {p["kb_id"]: p for p in data}
    assert by_kb["kb1"]["table_name"] == "secrets"
    assert by_kb["kb1"]["mode"] == "deny"
    assert by_kb["kb2"]["table_name"] is None


def test_upsert_policies_forbidden_for_non_owner(db):
    owner = _seed_user(db)
    recipient = _seed_user(db)
    intruder = _seed_user(db)
    proj = _seed_project(db, owner.id)
    _seed_share(db, owner.id, recipient.id, resource_id=proj.id)

    c = _client(db, intruder)
    body = {
        "resource_type": "project",
        "resource_id": proj.id,
        "user_id": recipient.id,
        "policies": [{"kb_id": "kb1", "table_name": None, "mode": "deny"}],
    }
    r = c.put("/api/access-policies", json=body)
    assert r.status_code == 403


def test_upsert_policies_allowed_for_admin(db):
    owner = _seed_user(db)
    recipient = _seed_user(db)
    admin = _seed_user(db, role="admin")
    proj = _seed_project(db, owner.id)
    _seed_share(db, owner.id, recipient.id, resource_id=proj.id)

    c = _client(db, admin)
    body = {
        "resource_type": "project",
        "resource_id": proj.id,
        "user_id": recipient.id,
        "policies": [{"kb_id": "kb1", "table_name": None, "mode": "deny"}],
    }
    r = c.put("/api/access-policies", json=body)
    assert r.status_code == 200, r.text


def test_upsert_rejects_invalid_mode(db):
    owner = _seed_user(db)
    recipient = _seed_user(db)
    proj = _seed_project(db, owner.id)
    _seed_share(db, owner.id, recipient.id, resource_id=proj.id)

    c = _client(db, owner)
    body = {
        "resource_type": "project",
        "resource_id": proj.id,
        "user_id": recipient.id,
        "policies": [{"kb_id": "kb1", "table_name": None, "mode": "bogus"}],
    }
    r = c.put("/api/access-policies", json=body)
    assert r.status_code == 422


def test_upsert_rejects_allow_columns_without_table(db):
    owner = _seed_user(db)
    recipient = _seed_user(db)
    proj = _seed_project(db, owner.id)
    _seed_share(db, owner.id, recipient.id, resource_id=proj.id)

    c = _client(db, owner)
    body = {
        "resource_type": "project",
        "resource_id": proj.id,
        "user_id": recipient.id,
        "policies": [{"kb_id": "kb1", "table_name": None, "mode": "allow_columns",
                      "column_allowlist": ["id"]}],
    }
    r = c.put("/api/access-policies", json=body)
    assert r.status_code == 422


def test_upsert_replaces_existing_policies(db):
    owner = _seed_user(db)
    recipient = _seed_user(db)
    proj = _seed_project(db, owner.id)
    _seed_share(db, owner.id, recipient.id, resource_id=proj.id)

    c = _client(db, owner)
    body1 = {
        "resource_type": "project", "resource_id": proj.id, "user_id": recipient.id,
        "policies": [{"kb_id": "kb1", "table_name": None, "mode": "deny"}],
    }
    assert c.put("/api/access-policies", json=body1).status_code == 200

    body2 = {
        "resource_type": "project", "resource_id": proj.id, "user_id": recipient.id,
        "policies": [{"kb_id": "kb2", "table_name": "Orders", "mode": "allow"}],
    }
    r = c.put("/api/access-policies", json=body2)
    assert r.status_code == 200
    data = r.json()["policies"]
    assert len(data) == 1
    assert data[0]["kb_id"] == "kb2"

    # Listing now only shows the new matrix (old soft-deleted).
    lst = c.get("/api/access-policies",
                params={"resource_type": "project", "resource_id": proj.id,
                        "user_id": recipient.id}).json()["policies"]
    assert [p["kb_id"] for p in lst] == ["kb2"]


def test_delete_policy_by_owner(db):
    owner = _seed_user(db)
    recipient = _seed_user(db)
    proj = _seed_project(db, owner.id)
    _seed_share(db, owner.id, recipient.id, resource_id=proj.id)

    c = _client(db, owner)
    body = {
        "resource_type": "project", "resource_id": proj.id, "user_id": recipient.id,
        "policies": [{"kb_id": "kb1", "table_name": None, "mode": "deny"}],
    }
    created = c.put("/api/access-policies", json=body).json()["policies"]
    pid = created[0]["id"]

    r = c.delete(f"/api/access-policies/{pid}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True

    lst = c.get("/api/access-policies",
                params={"resource_type": "project", "resource_id": proj.id,
                        "user_id": recipient.id}).json()["policies"]
    assert lst == []


def test_delete_policy_forbidden_for_non_owner(db):
    owner = _seed_user(db)
    recipient = _seed_user(db)
    intruder = _seed_user(db)
    proj = _seed_project(db, owner.id)
    _seed_share(db, owner.id, recipient.id, resource_id=proj.id)

    c = _client(db, owner)
    body = {
        "resource_type": "project", "resource_id": proj.id, "user_id": recipient.id,
        "policies": [{"kb_id": "kb1", "table_name": None, "mode": "deny"}],
    }
    created = c.put("/api/access-policies", json=body).json()["policies"]
    pid = created[0]["id"]

    c2 = _client(db, intruder)
    r = c2.delete(f"/api/access-policies/{pid}")
    assert r.status_code == 403


def test_preview_permissions(db):
    owner = _seed_user(db)
    recipient = _seed_user(db)
    proj = _seed_project(db, owner.id)
    kb_denied = _seed_kb(db, project_id=proj.id, name="secret_db")
    kb_restricted = _seed_kb(db, project_id=proj.id, name="sales_db")
    _seed_share(db, owner.id, recipient.id, resource_id=proj.id)

    c = _client(db, owner)
    body = {
        "resource_type": "project", "resource_id": proj.id, "user_id": recipient.id,
        "policies": [
            {"kb_id": kb_denied.id, "table_name": None, "mode": "deny"},
            {"kb_id": kb_restricted.id, "table_name": "Secrets", "mode": "deny"},
        ],
    }
    assert c.put("/api/access-policies", json=body).status_code == 200

    r = c.get("/api/access-policies/preview",
              params={"resource_type": "project", "resource_id": proj.id,
                      "user_id": recipient.id})
    assert r.status_code == 200, r.text
    kbs = {k["id"]: k for k in r.json()["kbs"]}
    assert kbs[kb_denied.id]["status"] == "denied"
    assert kbs[kb_restricted.id]["status"] == "restricted"
    assert kbs[kb_restricted.id]["blocked_tables"] == ["secrets"]


def test_preview_forbidden_for_non_owner(db):
    owner = _seed_user(db)
    recipient = _seed_user(db)
    intruder = _seed_user(db)
    proj = _seed_project(db, owner.id)
    _seed_share(db, owner.id, recipient.id, resource_id=proj.id)

    c = _client(db, intruder)
    r = c.get("/api/access-policies/preview",
              params={"resource_type": "project", "resource_id": proj.id,
                      "user_id": recipient.id})
    assert r.status_code == 403
