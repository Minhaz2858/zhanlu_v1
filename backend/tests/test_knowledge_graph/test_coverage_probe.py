"""Tests for catalog_indexer coverage probe (kb_table_meta.coverage_json).

Uses a mocked connector so no live warehouse is needed.
"""

from __future__ import annotations

import uuid

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import MagicMock, patch

from app.database import Base
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBTableMeta


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


def _make_kb(db):
    kb = KnowledgeBase(
        id=str(uuid.uuid4()), app_id="test-app", org_id="test-org",
        name="test-db", source_kind="db", db_type="mysql",
        host="localhost", port=3306, database_name="test",
        catalog_status="pending",
    )
    db.add(kb)
    db.commit()
    return kb


class TestCoverageProbe:
    def test_probes_min_max_of_date_column(self, db):
        kb = _make_kb(db)
        mock_conn = MagicMock()
        mock_conn.list_tables.return_value = ["shipments"]
        mock_conn.describe_table.return_value = {
            "schema": "", "type": "TABLE",
            "columns": [
                {"name": "id", "ordinal": 1, "type": "INT", "nullable": False,
                 "primary_key": True, "default": None},
                {"name": "ship_date", "ordinal": 2, "type": "DATE", "nullable": True,
                 "primary_key": False, "default": None},
            ],
        }
        mock_conn.execute.return_value = [["2025-01-01", "2025-12-31"]]  # [min, max]
        mock_conn.get_foreign_keys.return_value = []

        with patch("app.services.knowledge_graph.catalog_indexer.get_connector") as mock_gc, \
                patch("app.services.knowledge_graph.catalog_indexer.settings") as mock_settings:
            mock_settings.KG_COVERAGE_PROBE_ENABLED = True
            mock_gc.return_value.__enter__.return_value = mock_conn
            mock_gc.return_value.__exit__.return_value = False
            from app.services.knowledge_graph.catalog_indexer import (
                _introspect_tables_sync,
            )
            tables = _introspect_tables_sync(kb, db)

        # Persist and assert coverage_json was written.
        from app.services.knowledge_graph.catalog_indexer import _persist_catalog
        _persist_catalog(db, kb.id, tables)

        meta = db.query(KBTableMeta).filter(KBTableMeta.kb_id == kb.id).first()
        cov = meta.coverage_json
        assert cov is not None
        assert cov["date_column"] == "ship_date"
        assert cov["min_date"] == "2025-01-01"
        assert cov["max_date"] == "2025-12-31"
        assert "probed_at" in cov

    def test_no_temporal_column_no_coverage(self, db):
        kb = _make_kb(db)
        mock_conn = MagicMock()
        mock_conn.list_tables.return_value = ["config"]
        mock_conn.describe_table.return_value = {
            "schema": "", "type": "TABLE",
            "columns": [
                {"name": "key", "ordinal": 1, "type": "VARCHAR", "nullable": False,
                 "primary_key": True, "default": None},
                {"name": "value", "ordinal": 2, "type": "TEXT", "nullable": True,
                 "primary_key": False, "default": None},
            ],
        }
        mock_conn.execute.return_value = [[0]]
        mock_conn.get_foreign_keys.return_value = []

        with patch("app.services.knowledge_graph.catalog_indexer.get_connector") as mock_gc, \
                patch("app.services.knowledge_graph.catalog_indexer.settings") as mock_settings:
            mock_settings.KG_COVERAGE_PROBE_ENABLED = True
            mock_gc.return_value.__enter__.return_value = mock_conn
            mock_gc.return_value.__exit__.return_value = False
            from app.services.knowledge_graph.catalog_indexer import (
                _introspect_tables_sync,
            )
            tables = _introspect_tables_sync(kb, db)
            assert "coverage_json" not in tables[0] or tables[0].get("coverage_json") is None

    def test_probe_failure_is_non_fatal(self, db):
        """A probe timeout/error must not break introspection."""
        kb = _make_kb(db)
        mock_conn = MagicMock()
        mock_conn.list_tables.return_value = ["shipments"]
        mock_conn.describe_table.return_value = {
            "schema": "", "type": "TABLE",
            "columns": [
                {"name": "id", "ordinal": 1, "type": "INT", "nullable": False,
                 "primary_key": True, "default": None},
                {"name": "ship_date", "ordinal": 2, "type": "DATE", "nullable": True,
                 "primary_key": False, "default": None},
            ],
        }
        # execute raises → probe must swallow and return no coverage_json.
        mock_conn.execute.side_effect = RuntimeError("timeout")
        mock_conn.get_foreign_keys.return_value = []

        with patch("app.services.knowledge_graph.catalog_indexer.get_connector") as mock_gc, \
                patch("app.services.knowledge_graph.catalog_indexer.settings") as mock_settings:
            mock_settings.KG_COVERAGE_PROBE_ENABLED = True
            mock_gc.return_value.__enter__.return_value = mock_conn
            mock_gc.return_value.__exit__.return_value = False
            from app.services.knowledge_graph.catalog_indexer import (
                _introspect_tables_sync,
            )
            tables = _introspect_tables_sync(kb, db)

        assert tables[0].get("coverage_json") is None
