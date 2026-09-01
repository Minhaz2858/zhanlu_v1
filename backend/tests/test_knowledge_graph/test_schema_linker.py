"""Unit tests for schema_linker — mock Chroma collection, no warehouse."""

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


def _seed_catalog_tables(db, kb_id: str) -> list[str]:
    """Seed 3 tables + columns + relations for join-path tests."""
    import uuid as _uuid

    meta_ids = []
    for name, desc_zh in [("products", "产品表"), ("orders", "订单表"), ("customers", "客户表")]:
        m = KBTableMeta(
            id=str(_uuid.uuid4()),
            kb_id=kb_id,
            table_name=name,
            description_zh=desc_zh,
            table_type="TABLE",
            row_count=100,
        )
        db.add(m)
        db.flush()
        meta_ids.append(m.id)

    # Add FK: orders.product_id → products.id
    r = KBTableRelation(
        id=str(_uuid.uuid4()),
        kb_id=kb_id,
        source_table_meta_id=meta_ids[1],
        target_table_meta_id=meta_ids[0],
        relation_type="FK",
        source_columns=["product_id"],
        target_columns=["id"],
        confidence=1.0,
    )
    db.add(r)
    # Add FK: orders.customer_id → customers.id
    r2 = KBTableRelation(
        id=str(_uuid.uuid4()),
        kb_id=kb_id,
        source_table_meta_id=meta_ids[1],
        target_table_meta_id=meta_ids[2],
        relation_type="FK",
        source_columns=["customer_id"],
        target_columns=["id"],
        confidence=1.0,
    )
    db.add(r2)
    db.commit()
    return meta_ids


class TestRetrieveFromCatalog:
    """Test _retrieve_from_catalog with mock Chroma collection."""

    def test_returns_none_when_collection_missing(self):
        """If Chroma collection doesn't exist, return None (graceful fallback)."""
        from app.services.knowledge_graph.schema_linker import _retrieve_from_catalog

        with patch("app.services.document_ingestion.store._get_client") as mock_cl:
            mock_cl.return_value.get_collection.side_effect = Exception("not found")
            result = _retrieve_from_catalog("test query", "kb-1", top_k=5)
            assert result is None


class TestExpandJoins:
    """Test _expand_joins with real DB-backed relations."""

    def test_finds_direct_fk_joins(self, db):
        """Direct FK between retrieved tables is found."""
        import uuid as _uuid
        kb_id = str(_uuid.uuid4())

        meta_ids = _seed_catalog_tables(db, kb_id)

        # Retrieved tables: orders + products
        tables = [
            {"table_meta_id": meta_ids[1], "table_name": "orders"},
            {"table_meta_id": meta_ids[0], "table_name": "products"},
        ]

        from app.services.knowledge_graph.schema_linker import _expand_joins
        paths = _expand_joins(db, tables)

        assert len(paths) >= 1
        assert paths[0]["from_table"] == "orders"
        assert paths[0]["to_table"] == "products"
        assert "product_id" in paths[0]["on"]

    def test_no_self_joins(self, db):
        """A table with no FK to others returns empty."""
        import uuid as _uuid
        kb_id = str(_uuid.uuid4())

        meta_ids = _seed_catalog_tables(db, kb_id)

        tables = [
            {"table_meta_id": meta_ids[2], "table_name": "customers"},
        ]

        from app.services.knowledge_graph.schema_linker import _expand_joins
        paths = _expand_joins(db, tables)

        assert paths == []


class TestFormatSlice:
    """Test _format_slice token budgeting."""

    def test_respects_token_budget(self):
        tables = [
            {
                "table_name": "small_table",
                "description_zh": "Small table",
                "columns": [
                    {"name": f"col_{i}", "data_type": "TEXT", "is_nullable": True,
                     "is_primary_key": i == 0, "description_zh": ""}
                    for i in range(20)
                ],
            },
            {
                "table_name": "another_table",
                "description_zh": "Another table",
                "columns": [
                    {"name": "id", "data_type": "INT", "is_nullable": False,
                     "is_primary_key": True, "description_zh": ""},
                ],
            },
        ]
        join_paths = []

        from app.services.knowledge_graph.schema_linker import _format_slice
        result = _format_slice(tables, join_paths, token_budget=400)

        # With 20 columns per table and budget=400, first table fits,
        # second table may or may not (depending on exact token estimation)
        assert "small_table" in result

    def test_empty_tables_returns_header_only(self):
        from app.services.knowledge_graph.schema_linker import _format_slice
        result = _format_slice([], [], token_budget=800)
        assert "Semantic catalog" in result


class TestTokenEstimation:
    """Test _estimate_tokens rough heuristic."""

    def test_mixed_cjk_english(self):
        from app.services.knowledge_graph.schema_linker import _estimate_tokens
        text = "这是一个测试字符串 with English mixed in"
        tokens = _estimate_tokens(text)
        assert tokens >= 1
        # len(text) = ~30, // 2 ≈ 15
        assert 14 <= tokens <= 18
