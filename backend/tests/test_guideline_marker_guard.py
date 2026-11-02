"""Phase 7C T15 — manual guidelines can never masquerade as derived markers.

The business-rule bridge materializes enabled SQL rules as SemanticGuideline
rows titled ``[business-rule:{id}] {scope}`` and sweeps/reconciles rows by
that title prefix. The guidelines CRUD endpoints accepted arbitrary titles,
so a user could (a) create a manual row that the next sync clobbers, or
(b) PATCH-rename a derived row into a stale manual line that no sync can
retract. Both are now refused with 400.
"""

import uuid
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.knowledge_base import KnowledgeBase
from app.models.semantic_guideline import SemanticGuideline
from app.models.user import User
from app.deps import get_current_user_required, get_db
from app.routers.knowledge_bases import router

ORG = "org-acme"


@pytest.fixture()
def client():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        KnowledgeBase.__table__, SemanticGuideline.__table__,
        User.__table__])
    s = sessionmaker(bind=eng)()
    s.add(User(id="u1", org_id=ORG, email="a@t", full_name="A",
               role="admin", password_hash="x"))
    s.add(KnowledgeBase(id="kb-1", name="DB", org_id=ORG,
                        source_kind="database"))
    s.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api")

    def _get_db():
        yield s

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user_required] = lambda: s.get(
        User, "u1")
    c = TestClient(app)
    yield c, s
    s.close()
    eng.dispose()


def test_create_rejects_marker_prefixed_title(client):
    c, _ = client
    res = c.post("/api/apps/default-app/knowledge_bases/kb-1/guidelines", json={
        "title": "[business-rule:fake-id] orders",
        "content": "SELECT always id",
    })
    assert res.status_code == 400
    assert "business-rule" in res.json()["detail"]


def test_update_rejects_renaming_into_marker_namespace(client):
    c, s = client
    s.add(SemanticGuideline(
        id=str(uuid.uuid4()), org_id=ORG, datasource_id="kb-1",
        title="Return policy", content="30 days", kind="policy",
        is_enabled=True))
    s.commit()
    gid = s.query(SemanticGuideline).filter_by(
        datasource_id="kb-1").one().id
    res = c.patch(f"/api/apps/default-app/knowledge_bases/kb-1/guidelines/{gid}", json={
        "title": "[business-rule:br-1] orders",
    })
    assert res.status_code == 400


def test_normal_titles_still_accepted(client):
    c, _ = client
    res = c.post("/api/apps/default-app/knowledge_bases/kb-1/guidelines", json={
        "title": "Fiscal year convention", "content": "FY starts in April"})
    assert res.status_code in (200, 201), res.text
