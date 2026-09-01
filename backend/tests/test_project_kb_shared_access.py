"""Regression tests for C2: project-KB sharing uses correct ResourceShare columns."""
import os, uuid

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.database import Base
from app.models.resource_share import ResourceShare
from app.models.user import User
from app.models.project import Project
from app.models.knowledge_base import KnowledgeBase


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _seed_user(db, uid, name):
    u = User(id=uid, email=f"{uid}@t.com", full_name=name, role="user",
             password_hash="x", org_id="o", app_id="a")
    db.add(u)
    db.commit()
    return u


# ── C2: project-KB sharing ──

def test_shared_user_can_find_share(db):
    """C2: Query with shared_with_user_id + 'project' finds the share."""
    owner_id, shared_id = str(uuid.uuid4()), str(uuid.uuid4())
    _seed_user(db, owner_id, "Owner")
    _seed_user(db, shared_id, "Shared")

    proj = Project(id=str(uuid.uuid4()), name="P", created_by_id=owner_id,
                   org_id="o", app_id="a")
    db.add(proj)
    db.commit()

    share = ResourceShare(id=str(uuid.uuid4()), resource_type="project",
                          resource_id=proj.id, shared_with_user_id=shared_id,
                          access_level="read", org_id="o", app_id="a")
    db.add(share)
    db.commit()

    found = db.query(ResourceShare).filter(
        ResourceShare.resource_id == proj.id,
        ResourceShare.shared_with_user_id == shared_id,
        ResourceShare.resource_type == "project",
    ).first()
    assert found is not None
    assert found.shared_with_user_id == shared_id


def test_resource_share_no_user_id_column():
    """C2: ResourceShare must have shared_with_user_id, NOT user_id."""
    mapper = inspect(ResourceShare)
    cols = {c.key for c in mapper.columns}
    assert "shared_with_user_id" in cols
    assert "user_id" not in cols
