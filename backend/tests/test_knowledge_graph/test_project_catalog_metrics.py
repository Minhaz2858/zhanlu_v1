"""API tests for the per-project metric registry endpoints:

    GET  /api/apps/{app_id}/projects/{project_id}/catalog/metrics
    PUT  /api/apps/{app_id}/projects/{project_id}/catalog/metrics/{metric_id}
    POST /api/apps/{app_id}/projects/{project_id}/catalog/metrics/bootstrap

Covers listing, approval gating (only approved injected elsewhere, but the
endpoint returns both), edit by owner/admin, 403 for non-members, and 404
for missing metric.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fastapi.testclient import TestClient

from app.database import Base
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBTableMeta, ProjectMetric
from app.models.project import Project
from app.models.user import User
from app.routers.project_catalog import register_project_catalog_router


APP = "local-zhanlu-app"


@pytest.fixture
def db(tmp_path):
    db_file = tmp_path / f"pc_{uuid.uuid4().hex[:8]}.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture
def _user(db):
    u = User(id="u1", email="owner@x.com", full_name="Owner", password_hash="x", role="user")
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def _other_user(db):
    u = User(id="u2", email="other@x.com", full_name="Other", password_hash="x", role="user")
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def _admin(db):
    u = User(id="u3", email="admin@x.com", full_name="Admin", password_hash="x", role="admin")
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def _seed_project(db, _user):
    p = Project(id="proj-metrics", name="Metrics Project",
                app_id=APP, org_id="o", created_by_id=_user.id)
    db.add(p)
    kb = KnowledgeBase(id="kb-m", app_id=APP, org_id="o", name="Metrics DB",
                      source_kind="db", db_type="mysql", host="h", port=1,
                      database_name="d", project_id=p.id, catalog_status="pending")
    db.add(kb)
    db.add(KBTableMeta(
        id="kb-m-t1", kb_id="kb-m", table_name="sales", org_id="o", app_id=APP,
        coverage_json={"date_column": "d", "min_date": "2025-01-01",
                       "max_date": "2026-08-01", "probed_at": "x"},
    ))
    db.commit()
    return p


@pytest.fixture
def _client(db, _user):
    from fastapi import FastAPI
    from app.deps import get_current_user_required
    from app.database import get_db

    app = FastAPI()
    app.include_router(register_project_catalog_router())
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: _user
    return TestClient(app)


class TestMetricsEndpoints:
    def test_list_empty(self, _client, _seed_project):
        r = _client.get(f"/api/apps/{APP}/projects/{_seed_project.id}/catalog/metrics")
        assert r.status_code == 200
        body = r.json()
        assert body["metrics"] == []
        assert body["can_edit"] is True

    def test_list_returns_proposed_and_approved(self, _client, _seed_project, db):
        db.add(ProjectMetric(
            id="pm1", project_id=_seed_project.id, kb_id="kb-m", name="Revenue",
            aliases=["收入"], definition="d", sql_expression="SUM(x)",
            status="approved", source="user",
        ))
        db.add(ProjectMetric(
            id="pm2", project_id=_seed_project.id, kb_id="kb-m", name="Margin",
            aliases=["毛利率"], definition="d", sql_expression="SUM(m)",
            status="proposed", source="llm",
        ))
        db.commit()
        r = _client.get(f"/api/apps/{APP}/projects/{_seed_project.id}/catalog/metrics")
        assert r.status_code == 200
        names = {m["name"] for m in r.json()["metrics"]}
        assert {"Revenue", "Margin"} == names

    def test_approve_via_put(self, _client, _seed_project, db):
        db.add(ProjectMetric(
            id="pm3", project_id=_seed_project.id, kb_id="kb-m", name="Cost",
            aliases=["成本"], definition="d", sql_expression="SUM(c)",
            status="proposed", source="llm",
        ))
        db.commit()
        r = _client.put(
            f"/api/apps/{APP}/projects/{_seed_project.id}/catalog/metrics/pm3",
            json={"status": "approved"},
        )
        assert r.status_code == 200
        assert r.json()["metric"]["status"] == "approved"
        # DB reflects it.
        refreshed = db.query(ProjectMetric).filter(ProjectMetric.id == "pm3").first()
        assert refreshed.status == "approved"

    def test_put_edit_fields(self, _client, _seed_project, db):
        db.add(ProjectMetric(
            id="pm4", project_id=_seed_project.id, kb_id="kb-m", name="Margin",
            aliases=["毛利率"], definition="old", sql_expression="old",
            status="proposed", source="llm",
        ))
        db.commit()
        r = _client.put(
            f"/api/apps/{APP}/projects/{_seed_project.id}/catalog/metrics/pm4",
            json={"definition": "new", "sql_expression": "SUM(m)"},
        )
        assert r.status_code == 200
        assert r.json()["metric"]["definition"] == "new"
        assert r.json()["metric"]["sql_expression"] == "SUM(m)"

    def test_put_missing_metric_404(self, _client, _seed_project):
        r = _client.put(
            f"/api/apps/{APP}/projects/{_seed_project.id}/catalog/metrics/nope",
            json={"status": "approved"},
        )
        assert r.status_code == 404

    def test_bootstrap_creates_proposed(self, _client, _seed_project, db, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "KG_METRIC_BOOTSTRAP_ENABLED", True)

        async def fake_llm(*a, **k):
            return {
                "data": [{"name": "Revenue", "aliases": ["收入"],
                          "definition": "d", "sql_expression": "SUM(x)",
                          "query_pattern": "q", "unit": "",
                          "default_aggregation": "sum",
                          "bindings": [{"table": "sales"}]}]
            }
        from unittest.mock import patch
        with patch(
            "app.services.llm_service.call_llm", fake_llm
        ):
            r = _client.post(
                f"/api/apps/{APP}/projects/{_seed_project.id}/catalog/metrics/bootstrap"
            )
        assert r.status_code == 200
        assert r.json()["success"] is True
        assert len(r.json()["created"]) == 1
        rows = db.query(ProjectMetric).filter(
            ProjectMetric.project_id == _seed_project.id).all()
        assert len(rows) == 1
        assert rows[0].status == "proposed"


class TestMetricsAccessScoping:
    def test_non_member_gets_403(self, db, _seed_project, _other_user):
        from fastapi import FastAPI
        from app.deps import get_current_user_required
        from app.database import get_db

        app = FastAPI()
        app.include_router(register_project_catalog_router())
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user_required] = lambda: _other_user
        client = TestClient(app)
        r = client.get(
            f"/api/apps/{APP}/projects/{_seed_project.id}/catalog/metrics")
        assert r.status_code == 403

    def test_admin_can_edit(self, db, _seed_project, _admin):
        from fastapi import FastAPI
        from app.deps import get_current_user_required
        from app.database import get_db

        app = FastAPI()
        app.include_router(register_project_catalog_router())
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user_required] = lambda: _admin
        client = TestClient(app)

        db.add(ProjectMetric(
            id="pmA", project_id=_seed_project.id, kb_id="kb-m", name="Revenue",
            aliases=["收入"], definition="d", sql_expression="SUM(x)",
            status="proposed", source="llm",
        ))
        db.commit()
        r = client.put(
            f"/api/apps/{APP}/projects/{_seed_project.id}/catalog/metrics/pmA",
            json={"status": "approved"},
        )
        assert r.status_code == 200
        assert r.json()["metric"]["status"] == "approved"

    def test_shared_user_readonly_403_on_edit(self, db, _seed_project, _other_user):
        from fastapi import FastAPI
        from app.deps import get_current_user_required
        from app.database import get_db
        from app.models.resource_share import ResourceShare

        # Grant read-only share.
        db.add(ResourceShare(
            id="sh1", resource_id=_seed_project.id, resource_type="project",
            shared_with_user_id=_other_user.id, access_level="view",
        ))
        db.commit()

        app = FastAPI()
        app.include_router(register_project_catalog_router())
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user_required] = lambda: _other_user
        client = TestClient(app)

        db.add(ProjectMetric(
            id="pmB", project_id=_seed_project.id, kb_id="kb-m", name="Revenue",
            aliases=["收入"], definition="d", sql_expression="SUM(x)",
            status="proposed", source="llm",
        ))
        db.commit()

        # Read is allowed.
        r1 = client.get(
            f"/api/apps/{APP}/projects/{_seed_project.id}/catalog/metrics")
        assert r1.status_code == 200
        # Edit is forbidden (share is view/use-only).
        r2 = client.put(
            f"/api/apps/{APP}/projects/{_seed_project.id}/catalog/metrics/pmB",
            json={"status": "approved"},
        )
        assert r2.status_code == 403
