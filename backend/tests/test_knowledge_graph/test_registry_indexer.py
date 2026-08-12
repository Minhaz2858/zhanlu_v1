"""Resource Registry — upsert idempotency, visibility tiers, status transitions."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.resource_registry import ResourceRegistry
from app.services.knowledge_graph import registry_indexer as ri


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


PID = "proj-1"


class TestUpsert:
    def test_insert_then_upsert_is_idempotent(self, db):
        r1 = ri.upsert_resource(
            db, project_id=PID, resource_type="database",
            resource_id="kb-1", name="Warehouse", summary="v1",
        )
        db.commit()
        r2 = ri.upsert_resource(
            db, project_id=PID, resource_type="database",
            resource_id="kb-1", name="Warehouse", summary="v2",
            status="ready",
        )
        db.commit()
        rows = db.query(ResourceRegistry).filter_by(project_id=PID).all()
        assert len(rows) == 1
        assert rows[0].id == r1.id == r2.id
        assert rows[0].summary == "v2"
        assert rows[0].status == "ready"

    def test_different_types_same_resource_id_allowed(self, db):
        ri.upsert_resource(db, project_id=PID, resource_type="database",
                           resource_id="x", name="A")
        ri.upsert_resource(db, project_id=PID, resource_type="file",
                           resource_id="x", name="B")
        db.commit()
        assert db.query(ResourceRegistry).filter_by(project_id=PID).count() == 2

    def test_entities_and_visibility_stored(self, db):
        ri.upsert_resource(
            db, project_id=PID, resource_type="memory",
            resource_id="mem-1", name="Note", entities=["Alpha", "Beta"],
            visibility="user_private", owner_user_id="user-1",
        )
        db.commit()
        row = db.query(ResourceRegistry).filter_by(project_id=PID).one()
        assert row.entities == ["Alpha", "Beta"]
        assert row.visibility == "user_private"
        assert row.owner_user_id == "user-1"

    def test_last_indexed_at_set(self, db):
        ri.upsert_resource(db, project_id=PID, resource_type="database",
                           resource_id="kb-9", name="W")
        db.commit()
        row = db.query(ResourceRegistry).filter_by(project_id=PID).one()
        assert row.last_indexed_at is not None


class TestStatusTransitions:
    def test_mark_status(self, db):
        ri.upsert_resource(db, project_id=PID, resource_type="database",
                           resource_id="kb-1", name="W", status="pending")
        db.commit()
        ri.mark_status(db, PID, "database", "kb-1", "error")
        db.commit()
        row = db.query(ResourceRegistry).filter_by(project_id=PID).one()
        assert row.status == "error"

    def test_mark_status_missing_row_is_noop(self, db):
        ri.mark_status(db, PID, "database", "nope", "ready")  # must not raise


class TestVisibilityTiers:
    def _seed(self, db):
        ri.upsert_resource(db, project_id=PID, resource_type="database",
                           resource_id="kb-pub", name="Shared",
                           visibility="project")
        ri.upsert_resource(db, project_id=PID, resource_type="memory",
                           resource_id="mem-priv", name="Mine",
                           visibility="user_private", owner_user_id="user-1")
        ri.upsert_resource(db, project_id=PID, resource_type="report",
                           resource_id="rep-org", name="OrgReport",
                           visibility="org")
        db.commit()

    def test_owner_sees_everything(self, db):
        self._seed(db)
        rows = ri.list_project_resources(db, PID, viewer_user_id="user-1")
        assert len(rows) == 3

    def test_other_member_cannot_see_private(self, db):
        self._seed(db)
        rows = ri.list_project_resources(db, PID, viewer_user_id="user-2")
        names = {r.name for r in rows}
        assert names == {"Shared", "OrgReport"}

    def test_admin_sees_everything(self, db):
        self._seed(db)
        rows = ri.list_project_resources(
            db, PID, viewer_user_id="user-2", is_admin=True
        )
        assert len(rows) == 3

    def test_type_filter(self, db):
        self._seed(db)
        rows = ri.list_project_resources(
            db, PID, viewer_user_id="user-1", resource_type="database"
        )
        assert [r.name for r in rows] == ["Shared"]


class TestKnowledgeBaseIndexing:
    def test_index_knowledge_base(self, db):
        class _KB:
            id = "kb-1"
            name = "aipdp warehouse"
            db_type = "mysql"

        r = ri.index_knowledge_base(db, _KB(), project_id=PID, table_count=139)
        db.commit()
        assert r.resource_type == "database"
        assert r.status == "ready"
        assert "139" in (r.summary or "")
        row = db.query(ResourceRegistry).filter_by(project_id=PID).one()
        assert row.resource_id == "kb-1"


class TestAdditionalResourceIndexers:
    def test_index_report(self, db):
        r = ri.index_report(
            db,
            project_id=PID,
            report_id="rep-1",
            name="Weekly Sales Report",
            summary="Top customers and products",
            owner_user_id="u1",
        )
        db.commit()
        assert r.resource_type == "report"
        assert r.resource_id == "rep-1"
        assert r.name == "Weekly Sales Report"

    def test_index_conversation(self, db):
        r = ri.index_conversation(
            db,
            project_id=PID,
            conversation_id="conv-1",
            title="weekly sales ask",
            summary="user requested weekly sales trend",
        )
        db.commit()
        assert r.resource_type == "conversation"
        assert r.resource_id == "conv-1"
        assert "weekly" in (r.summary or "")

    def test_index_decision(self, db):
        r = ri.index_decision(
            db,
            project_id=PID,
            decision_id="dec-1",
            name="Pricing baseline",
            summary="Keep baseline price for Q3",
        )
        db.commit()
        assert r.resource_type == "decision"
        assert r.resource_id == "dec-1"

    def test_index_automation(self, db):
        r = ri.index_automation(
            db,
            project_id=PID,
            automation_id="auto-1",
            name="Weekly Sales Digest",
            summary="Runs every Monday",
        )
        db.commit()
        assert r.resource_type == "automation"
        assert r.resource_id == "auto-1"
