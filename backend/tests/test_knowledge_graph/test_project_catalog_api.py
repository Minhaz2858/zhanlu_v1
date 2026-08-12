"""Project Catalog API — overlay write/read, RBAC gating, registry listing."""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  register all models
from app.database import Base, get_db
from app.deps import get_current_user_required
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import (
    KBTableMeta,
    ProjectCatalogOverlay,
    ProjectEntity,
    ProjectEntityLink,
)
from app.models.project import Project
from app.models.resource_registry import ResourceRegistry
from app.models.user import User
from app.routers.project_catalog import register_project_catalog_router


@pytest.fixture
def db(tmp_path):
    db_file = tmp_path / f"test_{uuid.uuid4().hex[:8]}.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _user(db, role="user"):
    u = User(
        id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@t.io",
        full_name="t", role=role, password_hash="x",
        org_id="default-org", app_id="default-app",
    )
    db.add(u)
    db.commit()
    return u


def _client(db, user):
    app = FastAPI()
    app.include_router(register_project_catalog_router())
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: user
    return TestClient(app)


def _seed_project(db, owner):
    project = Project(
        id=str(uuid.uuid4()), name="P1", created_by_id=owner.id,
        org_id="default-org", app_id="default-app",
    )
    kb = KnowledgeBase(
        id=str(uuid.uuid4()), name="WH", source_kind="db", db_type="mysql",
        project_id=project.id, created_by_id=owner.id,
        org_id="default-org", app_id="default-app", catalog_status="ready",
    )
    db.add_all([project, kb])
    db.flush()
    meta = KBTableMeta(
        id=str(uuid.uuid4()), kb_id=kb.id, table_name="orders_fact",
        description_zh="订单事实表", row_count=1234,
        org_id="default-org", app_id="default-app",
    )
    db.add(meta)
    db.commit()
    return project, kb


APP = "local-zhanlu-app"


class TestCatalogTables:
    def test_owner_lists_tables(self, db):
        owner = _user(db)
        project, kb = _seed_project(db, owner)
        c = _client(db, owner)
        r = c.get(f"/api/apps/{APP}/projects/{project.id}/catalog/tables")
        assert r.status_code == 200
        body = r.json()
        assert body["can_edit"] is True
        assert len(body["tables"]) == 1
        assert body["tables"][0]["table_name"] == "orders_fact"
        assert body["tables"][0]["overlay"] is None
        assert body["kbs"][0]["catalog_status"] == "ready"

    def test_search_filters(self, db):
        owner = _user(db)
        project, kb = _seed_project(db, owner)
        c = _client(db, owner)
        r = c.get(
            f"/api/apps/{APP}/projects/{project.id}/catalog/tables",
            params={"search": "nomatch"},
        )
        assert r.json()["tables"] == []
        r = c.get(
            f"/api/apps/{APP}/projects/{project.id}/catalog/tables",
            params={"search": "订单"},
        )
        assert len(r.json()["tables"]) == 1

    def test_no_access_403(self, db):
        owner = _user(db)
        stranger = _user(db)
        project, kb = _seed_project(db, owner)
        c = _client(db, stranger)
        r = c.get(f"/api/apps/{APP}/projects/{project.id}/catalog/tables")
        assert r.status_code == 403

    def test_missing_project_404(self, db):
        u = _user(db)
        c = _client(db, u)
        r = c.get(f"/api/apps/{APP}/projects/nope/catalog/tables")
        assert r.status_code == 404


class TestOverlay:
    def test_owner_writes_and_reads_overlay(self, db):
        owner = _user(db)
        project, kb = _seed_project(db, owner)
        c = _client(db, owner)
        r = c.put(
            f"/api/apps/{APP}/projects/{project.id}/catalog/overlay",
            json={
                "kb_id": kb.id, "table_name": "orders_fact",
                "alias": "订单表", "description": " curated desc ",
                "metric_definition": "GMV = 净额",
            },
        )
        assert r.status_code == 200
        r2 = c.get(f"/api/apps/{APP}/projects/{project.id}/catalog/tables")
        ov = r2.json()["tables"][0]["overlay"]
        assert ov["alias"] == "订单表"
        assert ov["metric_definition"] == "GMV = 净额"

    def test_overlay_upsert_updates_in_place(self, db):
        owner = _user(db)
        project, kb = _seed_project(db, owner)
        c = _client(db, owner)
        for desc in ("v1", "v2"):
            c.put(
                f"/api/apps/{APP}/projects/{project.id}/catalog/overlay",
                json={"kb_id": kb.id, "table_name": "orders_fact", "description": desc},
            )
        rows = db.query(ProjectCatalogOverlay).filter_by(project_id=project.id).all()
        assert len(rows) == 1
        assert rows[0].description == "v2"

    def test_non_owner_cannot_write(self, db):
        owner = _user(db)
        stranger = _user(db)
        project, kb = _seed_project(db, owner)
        # grant stranger read access via ResourceShare
        from app.models.resource_share import ResourceShare

        db.add(ResourceShare(
            id=str(uuid.uuid4()), resource_type="project", resource_id=project.id,
            shared_with_user_id=stranger.id, created_by_id=owner.id,
            org_id="default-org", app_id="default-app",
        ))
        db.commit()
        c = _client(db, stranger)
        r = c.put(
            f"/api/apps/{APP}/projects/{project.id}/catalog/overlay",
            json={"kb_id": kb.id, "table_name": "orders_fact", "description": "x"},
        )
        assert r.status_code == 403
        # but reads work
        r2 = c.get(f"/api/apps/{APP}/projects/{project.id}/catalog/tables")
        assert r2.status_code == 200
        assert r2.json()["can_edit"] is False


class TestRegistryResources:
    def test_visibility_enforced_via_api(self, db):
        owner = _user(db)
        member = _user(db)
        project, kb = _seed_project(db, owner)
        db.add_all([
            ResourceRegistry(
                id=str(uuid.uuid4()), project_id=project.id,
                resource_type="database", resource_id=kb.id, name="WH",
                visibility="project", status="ready",
                org_id="default-org", app_id="default-app",
            ),
            ResourceRegistry(
                id=str(uuid.uuid4()), project_id=project.id,
                resource_type="memory", resource_id="m1", name="PrivateNote",
                visibility="user_private", owner_user_id=owner.id, status="ready",
                org_id="default-org", app_id="default-app",
            ),
        ])
        from app.models.resource_share import ResourceShare

        db.add(ResourceShare(
            id=str(uuid.uuid4()), resource_type="project", resource_id=project.id,
            shared_with_user_id=member.id, created_by_id=owner.id,
            org_id="default-org", app_id="default-app",
        ))
        db.commit()

        r_owner = _client(db, owner).get(
            f"/api/apps/{APP}/projects/{project.id}/registry/resources"
        )
        assert len(r_owner.json()["resources"]) == 2

        r_member = _client(db, member).get(
            f"/api/apps/{APP}/projects/{project.id}/registry/resources"
        )
        names = [r["name"] for r in r_member.json()["resources"]]
        assert names == ["WH"]

    def test_type_filter(self, db):
        owner = _user(db)
        project, kb = _seed_project(db, owner)
        db.add(ResourceRegistry(
            id=str(uuid.uuid4()), project_id=project.id,
            resource_type="database", resource_id=kb.id, name="WH",
            visibility="project", status="ready",
            org_id="default-org", app_id="default-app",
        ))
        db.commit()
        c = _client(db, owner)
        r = c.get(
            f"/api/apps/{APP}/projects/{project.id}/registry/resources",
            params={"resource_type": "file"},
        )
        assert r.json()["resources"] == []


class TestEntities:
    def test_empty_entities(self, db):
        owner = _user(db)
        project, kb = _seed_project(db, owner)
        c = _client(db, owner)
        r = c.get(f"/api/apps/{APP}/projects/{project.id}/catalog/entities")
        assert r.status_code == 200
        assert r.json()["entities"] == []


class TestKnowledgeMap:
    def test_owner_gets_resource_general_knowledge_map(self, db):
        owner = _user(db)
        project, kb = _seed_project(db, owner)

        db.add_all([
            ResourceRegistry(
                id=str(uuid.uuid4()),
                project_id=project.id,
                resource_type="database",
                resource_id=kb.id,
                name="WH",
                summary="mysql warehouse",
                entities=["orders", "gmv"],
                visibility="project",
                status="ready",
                org_id="default-org",
                app_id="default-app",
            ),
            ResourceRegistry(
                id=str(uuid.uuid4()),
                project_id=project.id,
                resource_type="file",
                resource_id="f1",
                name="pricing_rules.xlsx",
                summary="discount tiers",
                entities=["pricing", "discount"],
                visibility="project",
                status="ready",
                org_id="default-org",
                app_id="default-app",
            ),
            ResourceRegistry(
                id=str(uuid.uuid4()),
                project_id=project.id,
                resource_type="conversation",
                resource_id="c1",
                name="weekly sales ask",
                summary="user asked for weekly sales report",
                visibility="project",
                status="ready",
                org_id="default-org",
                app_id="default-app",
            ),
            ResourceRegistry(
                id=str(uuid.uuid4()),
                project_id=project.id,
                resource_type="memory",
                resource_id="m1",
                name="GMV definition",
                summary="GMV excludes refunds",
                visibility="project",
                status="ready",
                org_id="default-org",
                app_id="default-app",
            ),
        ])

        metric = ProjectEntity(
            id=str(uuid.uuid4()),
            project_id=project.id,
            name="Weekly Sales",
            aliases=["weekly revenue"],
            entity_type="metric",
            description="Weekly net sales",
            source="memory",
            org_id="default-org",
            app_id="default-app",
        )
        customer = ProjectEntity(
            id=str(uuid.uuid4()),
            project_id=project.id,
            name="Distributor",
            aliases=["channel customer"],
            entity_type="customer",
            description="Distribution account",
            source="memory",
            org_id="default-org",
            app_id="default-app",
        )
        db.add_all([metric, customer])
        db.flush()

        db.add_all([
            ProjectEntityLink(
                id=str(uuid.uuid4()),
                entity_id=metric.id,
                target_type="table",
                target_id="orders_fact",
                confidence=0.95,
                source="embedding",
                org_id="default-org",
                app_id="default-app",
            ),
            ProjectEntityLink(
                id=str(uuid.uuid4()),
                entity_id=metric.id,
                target_type="file",
                target_id="pricing_rules.xlsx",
                confidence=0.87,
                source="llm",
                org_id="default-org",
                app_id="default-app",
            ),
        ])
        db.commit()

        c = _client(db, owner)
        r = c.get(f"/api/apps/{APP}/projects/{project.id}/knowledge-map")
        assert r.status_code == 200
        body = r.json()

        assert body["can_edit"] is True
        assert body["summary"]["resource_count"] >= 4
        assert body["summary"]["entity_count"] == 2
        assert body["resources_by_type"]["database"][0]["name"] == "WH"
        assert body["resources_by_type"]["file"][0]["name"] == "pricing_rules.xlsx"
        assert body["knowledge_areas"]
        assert body["entities_by_type"]["metric"][0]["name"] == "Weekly Sales"
        assert len(body["entities_by_type"]["metric"][0]["links"]) == 2

    def test_member_sees_project_resources_not_private(self, db):
        owner = _user(db)
        member = _user(db)
        project, kb = _seed_project(db, owner)

        from app.models.resource_share import ResourceShare

        db.add(ResourceShare(
            id=str(uuid.uuid4()),
            resource_type="project",
            resource_id=project.id,
            shared_with_user_id=member.id,
            created_by_id=owner.id,
            org_id="default-org",
            app_id="default-app",
        ))
        db.add_all([
            ResourceRegistry(
                id=str(uuid.uuid4()),
                project_id=project.id,
                resource_type="database",
                resource_id=kb.id,
                name="WH",
                visibility="project",
                status="ready",
                org_id="default-org",
                app_id="default-app",
            ),
            ResourceRegistry(
                id=str(uuid.uuid4()),
                project_id=project.id,
                resource_type="memory",
                resource_id="priv-1",
                name="owner private memory",
                visibility="user_private",
                owner_user_id=owner.id,
                status="ready",
                org_id="default-org",
                app_id="default-app",
            ),
        ])
        db.commit()

        c = _client(db, member)
        r = c.get(f"/api/apps/{APP}/projects/{project.id}/knowledge-map")
        assert r.status_code == 200
        body = r.json()

        assert body["can_edit"] is False
        assert body["summary"]["resource_count"] == 1
        assert "memory" not in body["resources_by_type"]

    def test_missing_project_and_no_access(self, db):
        owner = _user(db)
        stranger = _user(db)
        project, kb = _seed_project(db, owner)

        c1 = _client(db, owner)
        r1 = c1.get(f"/api/apps/{APP}/projects/nope/knowledge-map")
        assert r1.status_code == 404

        c2 = _client(db, stranger)
        r2 = c2.get(f"/api/apps/{APP}/projects/{project.id}/knowledge-map")
        assert r2.status_code == 403
