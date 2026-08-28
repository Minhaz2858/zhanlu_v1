"""B1: scoped KB copy binding — clone_kb_for_project isolation test.

Verifies the app isolation rule for binding the 'Market Research Data' KB to
the C5_C9 project: `clone_kb_for_project` must create a SCOPED COPY (new row,
new id) bound to the target project and must NEVER repoint the original KB
away from its source project (inverse isolation check), and `_bound_kbs` for
the target project must return the copy.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  register all models
from app.models.knowledge_base import KnowledgeBase
from app.models.project import Project
from app.routers.project_catalog import _bound_kbs, clone_kb_for_project


# Isolated in-memory SQLite engine for this test module (StaticPool so every
# session via the sessionmaker sees the same single connection).
_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(_engine)
_TestSession = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture
def db():
    Base.metadata.drop_all(_engine)
    Base.metadata.create_all(_engine)
    s = _TestSession()
    try:
        yield s
    finally:
        s.close()


def _make_kb(project_id, **kw):
    """Mirror of the real 'Market Research Data' row shape."""
    kb = KnowledgeBase(
        name="Market Research Data",
        project_id=project_id,
        description="Market research reports and industry analysis",
        type="vector_db",
        source_kind="file",
        file_type="pdf",
        item_count=128,
        status="active",
        created_by_id="user-1",
        org_id="default-org",
        app_id="default-app",
        **kw,
    )
    return kb


def test_clone_kb_for_project_scoped_copy(db):
    p1 = str(uuid.uuid4())  # Data Analysis
    p2 = str(uuid.uuid4())  # C5_C9

    src = _make_kb(
        p1,
        username="readonly_user",
        password="s3cret-value",
        api_url="https://db.internal:5432",
    )
    db.add(src)
    db.commit()
    db.refresh(src)

    project_p2 = Project(id=p2, name="C5_C9", created_by_id="user-1")
    db.add(project_p2)
    db.commit()

    copy = clone_kb_for_project(db, src.id, p2)

    # The helper does NOT commit — the caller owns the transaction, so the
    # new row must not be visible to a fresh query yet.
    assert db.query(KnowledgeBase).count() == 1
    db.commit()
    db.refresh(copy)
    assert db.query(KnowledgeBase).count() == 2

    # New row exists, bound to the NEW project, same content fields
    assert copy.id != src.id
    assert copy.project_id == p2
    assert copy.name == src.name
    assert copy.description == src.description
    assert copy.type == src.type
    assert copy.source_kind == src.source_kind
    assert copy.file_type == src.file_type
    assert copy.item_count == src.item_count
    assert copy.status == src.status
    assert copy.created_by_id == src.created_by_id

    # Credential isolation: username rides along but the password NEVER
    # crosses project boundaries.
    assert copy.username == src.username
    assert copy.password is None
    assert copy.api_url == src.api_url

    # Inverse isolation: the original KB is STILL bound to P1 (never repointed)
    db.expire_all()
    original = db.query(KnowledgeBase).filter(KnowledgeBase.id == src.id).one()
    assert original.project_id == p1

    # _bound_kbs for P2 (C5_C9) returns exactly the copy
    bound_p2 = _bound_kbs(db, project_p2)
    assert [kb.id for kb in bound_p2] == [copy.id]

    # and the source project still sees its own KB
    project_p1 = Project(id=p1, name="Data Analysis")
    bound_p1 = _bound_kbs(db, project_p1)
    assert [kb.id for kb in bound_p1] == [src.id]


def test_clone_kb_for_project_missing_source_raises(db):
    with pytest.raises(ValueError):
        clone_kb_for_project(db, str(uuid.uuid4()), str(uuid.uuid4()))


def test_clone_kb_for_project_soft_deleted_source(db):
    p1 = str(uuid.uuid4())
    p2 = str(uuid.uuid4())

    src = _make_kb(p1)
    src.is_deleted = True
    db.add(src)
    db.commit()

    # Default: soft-deleted sources are refused (safe default).
    with pytest.raises(ValueError):
        clone_kb_for_project(db, src.id, p2)

    # include_deleted=True: scoped copy still created; source untouched.
    copy = clone_kb_for_project(db, src.id, p2, include_deleted=True)
    db.commit()
    db.refresh(copy)
    assert copy.id != src.id
    assert copy.project_id == p2
    assert copy.is_deleted is False
    assert copy.name == src.name
    assert copy.item_count == src.item_count
    assert copy.password is None

    db.expire_all()
    original = db.query(KnowledgeBase).filter(KnowledgeBase.id == src.id).one()
    assert original.project_id == p1
    assert original.is_deleted is True
