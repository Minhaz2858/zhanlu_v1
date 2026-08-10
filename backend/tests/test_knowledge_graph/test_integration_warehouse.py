"""Integration test — full catalog flow against the real aipdp_data_warehouse_prod.

Skipped when EDIA_MYSQL_URL is absent (CI / no warehouse access).
Run manually with the docker backend up:
    docker restart zhanlu-backend
    backend/venv/bin/python -m pytest tests/test_knowledge_graph/test_integration_warehouse.py -v

Covers: introspect -> describe (LLM tolerated) -> persist -> embed -> link.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from urllib.parse import unquote, urlparse

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta, KBTableRelation
from app.services.knowledge_graph.catalog_indexer import index_kb_catalog
from app.services.knowledge_graph.schema_linker import link_schema

EDIA_MYSQL_URL = os.getenv("EDIA_MYSQL_URL", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not EDIA_MYSQL_URL,
        reason="EDIA_MYSQL_URL not set — warehouse not reachable in this environment",
    ),
]


def _parse_url(url: str) -> dict:
    """Parse mysql+pymysql://user:pass@host:port/dbname into KB fields."""
    p = urlparse(url)
    return {
        "host": p.hostname or "localhost",
        "port": p.port or 3306,
        "database_name": (p.path or "/").lstrip("/"),
        "username": unquote(p.username or ""),
        "password": unquote(p.password or ""),
    }


@pytest.fixture
def db(tmp_path):
    """Fresh SQLite file for catalog persistence (catalog rows live here)."""
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


async def test_full_catalog_flow(db):
    """End-to-end: index the real warehouse, then link a real question."""
    from app.config import settings

    conn = _parse_url(EDIA_MYSQL_URL)
    kb = KnowledgeBase(
        id=str(uuid.uuid4()),
        app_id="test-app",
        org_id="test-org",
        name="aipdp_data_warehouse_prod-integration",
        source_kind="db",
        db_type="mysql",
        host=conn["host"],
        port=conn["port"],
        database_name=conn["database_name"],
        username=conn["username"],
        password=conn["password"],
        catalog_status="pending",
    )
    db.add(kb)
    db.commit()

    # 1-2. Index the full warehouse (LLM descriptions tolerated — indexer catches LLM errors).
    await index_kb_catalog(kb, db)
    db.commit()

    # 3. Table count >= 100 (warehouse has 139 tables).
    table_count = db.query(KBTableMeta).filter(KBTableMeta.kb_id == kb.id).count()
    assert table_count >= 100, f"expected >=100 tables, got {table_count}"

    # 4. Columns > tables (KBColumnMeta joins via table_meta_id).
    table_ids = [t.id for t in db.query(KBTableMeta).filter(KBTableMeta.kb_id == kb.id).all()]
    col_count = (
        db.query(KBColumnMeta).filter(KBColumnMeta.table_meta_id.in_(table_ids)).count()
        if table_ids
        else 0
    )
    assert col_count > table_count, f"columns={col_count} should exceed tables={table_count}"

    # 5. FKs exist.
    rel_count = db.query(KBTableRelation).filter(KBTableRelation.kb_id == kb.id).count()
    assert rel_count > 0, "expected at least one FK relation in the warehouse"

    # 6. Status ready.
    db.refresh(kb)
    assert kb.catalog_status == "ready", f"catalog_status={kb.catalog_status}"

    # 7-9. Link a real question against the populated catalog.
    old = settings.SCHEMA_LINKING_ENABLED
    settings.SCHEMA_LINKING_ENABLED = True
    try:
        result = await link_schema("最近乙烯价格走势如何？", [kb.id], db, top_k=8)
    finally:
        settings.SCHEMA_LINKING_ENABLED = old

    assert result is not None, "link_schema returned None for a populated catalog"
    assert result.get("tables"), "expected at least one retrieved table"
    top_tables = [t["table_name"] for t in result["tables"]]
    # md_t_lz_price is the EDIA price table — should surface in the top-K results.
    assert any("md_t_lz_price" in name for name in top_tables), (
        f"md_t_lz_price not in top matches: {top_tables[:8]}"
    )
    slice_text = result.get("slice_text") or ""
    assert len(slice_text) > 0, "slice_text should be non-empty"
    assert "CREATE TABLE" in slice_text or "md_t_lz_price" in slice_text, (
        "slice_text should contain DDL or the matched table"
    )
