"""Unit tests for catalog_indexer — SQLite fixture, mock connector, no Chroma."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBTableMeta, KBColumnMeta, KBTableRelation


@pytest.fixture
def db(tmp_path):
    """Fresh SQLite file for each test."""
    import uuid
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


def _make_kb(db) -> KnowledgeBase:
    """Minimal KB record for testing."""
    import uuid as _uuid
    kb = KnowledgeBase(
        id=str(_uuid.uuid4()),
        app_id="test-app",
        org_id="test-org",
        name="test-db",
        source_kind="db",
        db_type="mysql",
        host="localhost",
        port=3306,
        database_name="test",
        catalog_status="pending",
    )
    db.add(kb)
    db.commit()
    return kb


class TestIntrospectTablesSync:
    """Test _introspect_tables_sync with a mock connector."""

    def test_discovers_tables(self, db):
        """Mock list_tables + describe_table → returns structured table list."""
        kb = _make_kb(db)
        mock_conn = MagicMock()
        mock_conn.list_tables.return_value = ["products", "orders"]
        mock_conn.describe_table.side_effect = [
            {
                "schema": "public",
                "type": "TABLE",
                "columns": [
                    {"name": "id", "ordinal": 1, "type": "INT", "nullable": False, "primary_key": True, "default": None},
                    {"name": "name", "ordinal": 2, "type": "VARCHAR", "nullable": True, "primary_key": False, "default": None},
                ],
            },
            {
                "schema": "public",
                "type": "TABLE",
                "columns": [
                    {"name": "id", "ordinal": 1, "type": "INT", "nullable": False, "primary_key": True, "default": None},
                    {"name": "product_id", "ordinal": 2, "type": "INT", "nullable": False, "primary_key": False, "default": None},
                ],
            },
        ]
        mock_conn.execute.return_value = [[5]]  # row count
        mock_conn.get_foreign_keys.return_value = []

        with patch("app.services.knowledge_graph.catalog_indexer.get_connector") as mock_gc:
            mock_gc.return_value.__enter__.return_value = mock_conn
            mock_gc.return_value.__exit__.return_value = False

            from app.services.knowledge_graph.catalog_indexer import _introspect_tables_sync
            tables = _introspect_tables_sync(kb, db)

        assert len(tables) == 2
        assert tables[0]["table_name"] == "products"
        assert len(tables[0]["columns"]) == 2
        assert tables[1]["table_name"] == "orders"

    def test_caps_at_max_tables(self, db):
        """Tables beyond MAX_TABLES are dropped."""
        kb = _make_kb(db)
        mock_conn = MagicMock()
        many = [f"t{i}" for i in range(500)]
        mock_conn.list_tables.return_value = many
        mock_conn.describe_table.return_value = {
            "schema": "", "type": "TABLE", "columns": [],
        }
        mock_conn.execute.return_value = [[0]]
        mock_conn.get_foreign_keys.return_value = []

        with patch("app.services.knowledge_graph.catalog_indexer.get_connector") as mock_gc:
            mock_gc.return_value.__enter__.return_value = mock_conn
            mock_gc.return_value.__exit__.return_value = False

            from app.services.knowledge_graph.catalog_indexer import _introspect_tables_sync, MAX_TABLES
            tables = _introspect_tables_sync(kb, db)

        assert len(tables) == MAX_TABLES


class TestPersistCatalog:
    """Test _persist_catalog UPSERT idempotent."""

    def test_persists_new_tables_and_columns(self, db):
        kb = _make_kb(db)
        tables = [
            {
                "schema_name": "", "table_name": "items",
                "table_type": "TABLE", "row_count": 100,
                "description_zh": "物品表", "description_en": "Items table",
                "columns": [
                    {"column_name": "id", "ordinal": 1, "data_type": "INT",
                     "is_nullable": False, "is_primary_key": True, "default_value": None,
                     "description_zh": "主键", "description_en": "Primary key"},
                    {"column_name": "name", "ordinal": 2, "data_type": "VARCHAR",
                     "is_nullable": True, "is_primary_key": False, "default_value": None,
                     "description_zh": "名称", "description_en": "Name"},
                ],
                "foreign_keys": [],
            }
        ]
        from app.services.knowledge_graph.catalog_indexer import _persist_catalog
        _persist_catalog(db, kb.id, tables)

        # Verify table meta
        meta = db.query(KBTableMeta).filter(KBTableMeta.kb_id == kb.id).all()
        assert len(meta) == 1
        assert meta[0].table_name == "items"
        assert meta[0].description_zh == "物品表"

        # Verify column meta
        cols = db.query(KBColumnMeta).filter(KBColumnMeta.table_meta_id == meta[0].id).all()
        assert len(cols) == 2
        assert cols[0].column_name == "id"
        assert cols[1].column_name == "name"

    def test_upsert_idempotent(self, db):
        """Re-running _persist_catalog updates existing rows instead of duplicating."""
        kb = _make_kb(db)
        tables = [
            {
                "schema_name": "", "table_name": "items",
                "table_type": "TABLE", "row_count": 100,
                "description_zh": "物品表", "description_en": "Items table",
                "columns": [
                    {"column_name": "id", "ordinal": 1, "data_type": "INT",
                     "is_nullable": False, "is_primary_key": True, "default_value": None,
                     "description_zh": "主键", "description_en": "Primary key"},
                ],
                "foreign_keys": [],
            }
        ]
        from app.services.knowledge_graph.catalog_indexer import _persist_catalog

        # First pass
        _persist_catalog(db, kb.id, tables)
        first_count = db.query(KBTableMeta).filter(KBTableMeta.kb_id == kb.id).count()

        # Second pass — update description
        tables[0]["description_zh"] = "项目管理表"
        _persist_catalog(db, kb.id, tables)
        second_count = db.query(KBTableMeta).filter(KBTableMeta.kb_id == kb.id).count()

        assert first_count == second_count == 1
        meta = db.query(KBTableMeta).filter(KBTableMeta.kb_id == kb.id).first()
        assert meta.description_zh == "项目管理表"

    def test_fk_persist(self, db):
        """Foreign keys are persisted to kb_table_relation."""
        kb = _make_kb(db)
        tables = [
            {
                "schema_name": "", "table_name": "orders",
                "table_type": "TABLE", "row_count": 10,
                "columns": [],
                "foreign_keys": [
                    {"column": "product_id", "ref_schema": "", "ref_table": "products", "ref_column": "id"},
                ],
            },
            {
                "schema_name": "", "table_name": "products",
                "table_type": "TABLE", "row_count": 5,
                "columns": [],
                "foreign_keys": [],
            },
        ]
        from app.services.knowledge_graph.catalog_indexer import _persist_catalog, _persist_relations

        _persist_catalog(db, kb.id, tables)
        _persist_relations(db, kb.id, tables)

        rels = db.query(KBTableRelation).filter(KBTableRelation.kb_id == kb.id).all()
        assert len(rels) == 1
        assert rels[0].relation_type == "FK"
        assert rels[0].source_columns == ["product_id"]
        assert rels[0].target_columns == ["id"]


class TestErrorHandling:
    """Test that errors set catalog_status to 'error'."""

    @pytest.mark.asyncio
    async def test_error_sets_status(self, db):
        kb = _make_kb(db)
        # Force connector to raise
        with patch("app.services.knowledge_graph.catalog_indexer.get_connector") as mock_gc:
            mock_gc.side_effect = RuntimeError("connection failed")

            from app.services.knowledge_graph.catalog_indexer import index_kb_catalog
            await index_kb_catalog(kb, db)

        db.refresh(kb)
        assert kb.catalog_status == "error"

    @pytest.mark.asyncio
    async def test_list_tables_failure_sets_error_not_ready(self, db):
        """Connection-level list_tables failure must NOT silently mark KB 'ready'."""
        kb = _make_kb(db)
        mock_conn = MagicMock()
        mock_conn.list_tables.side_effect = RuntimeError("connection refused")

        with patch("app.services.knowledge_graph.catalog_indexer.get_connector") as mock_gc:
            mock_gc.return_value.__enter__.return_value = mock_conn
            mock_gc.return_value.__exit__.return_value = False

            from app.services.knowledge_graph.catalog_indexer import index_kb_catalog
            await index_kb_catalog(kb, db)

        db.refresh(kb)
        assert kb.catalog_status == "error"
