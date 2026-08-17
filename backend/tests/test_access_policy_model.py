"""Model-level tests for ResourceAccessPolicy.

Covers: column round-trip, defaults, tablename, and cascade soft-delete
when a share is revoked.
"""

import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal, get_db
from app.deps import get_current_user_required
from app.models.project import Project
from app.models.resource_share import ResourceShare
from app.models.resource_access_policy import ResourceAccessPolicy
from app.models.user import User
from app.routers.resource_shares import router as shares_router
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


def _seed_share(db, owner_id, recipient_id, resource_type="project", resource_id=None):
    s = ResourceShare(resource_type=resource_type,
                      resource_id=resource_id or str(uuid.uuid4()),
                      shared_with_user_id=recipient_id,
                      access_level="use",
                      created_by_id=owner_id)
    db.add(s)
    db.commit()
    return s


def test_policy_columns_round_trip(db):
    owner = _seed_user(db)
    recipient = _seed_user(db)
    share = _seed_share(db, owner.id, recipient.id)

    p = ResourceAccessPolicy(
        resource_share_id=share.id,
        resource_type="project",
        resource_id=share.resource_id,
        user_id=recipient.id,
        kb_id=str(uuid.uuid4()),
        table_name="Orders",
        mode="allow_columns",
        column_allowlist=["id", "amount"],
        row_filter={"region": "cn"},
        created_by_id=owner.id,
    )
    db.add(p)
    db.commit()
    db.refresh(p)

    assert p.id
    assert p.resource_share_id == share.id
    assert p.resource_type == "project"
    assert p.resource_id == share.resource_id
    assert p.user_id == recipient.id
    assert p.mode == "allow_columns"
    assert p.column_allowlist == ["id", "amount"]
    assert p.row_filter == {"region": "cn"}
    assert p.is_deleted is False


def test_default_mode_is_allow(db):
    owner = _seed_user(db)
    recipient = _seed_user(db)
    share = _seed_share(db, owner.id, recipient.id)

    p = ResourceAccessPolicy(
        resource_share_id=share.id,
        resource_type="project",
        resource_id=share.resource_id,
        user_id=recipient.id,
    )
    db.add(p)
    db.commit()
    db.refresh(p)

    assert p.mode == "allow"


def test_tablename(db):
    assert ResourceAccessPolicy.__tablename__ == "resource_access_policies"


def test_cascade_soft_delete_on_share_revoke(db):
    """Revoking a share must soft-delete its linked policies."""
    owner = _seed_user(db)
    recipient = _seed_user(db)
    proj = _seed_project(db, owner.id)
    share = _seed_share(db, owner.id, recipient.id,
                        resource_type="project", resource_id=proj.id)

    p = ResourceAccessPolicy(
        resource_share_id=share.id,
        resource_type="project",
        resource_id=proj.id,
        user_id=recipient.id,
        kb_id=str(uuid.uuid4()),
        table_name="Orders",
        mode="deny",
    )
    db.add(p)
    db.commit()

    # Revoke via the router endpoint (owner-authenticated).
    app = FastAPI()
    app.include_router(shares_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: owner
    client = TestClient(app)

    r = client.delete(f"/api/shares/{share.id}")
    assert r.status_code == 200, r.text

    db.expire_all()
    active = (db.query(ResourceAccessPolicy)
              .filter(ResourceAccessPolicy.resource_share_id == share.id,
                      ResourceAccessPolicy.is_deleted == False)  # noqa: E712
              .all())
    assert active == []
    # The row still exists but is soft-deleted.
    all_rows = (db.query(ResourceAccessPolicy)
                .filter(ResourceAccessPolicy.resource_share_id == share.id)
                .all())
    assert len(all_rows) == 1
    assert all_rows[0].is_deleted is True
