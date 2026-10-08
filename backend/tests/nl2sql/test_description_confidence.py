"""Description confidence scoring + human correction path (2026-09-22, OCI #1).

The onboarding LLM writes "未知" for columns it cannot name — and nothing
distinguished those guesses from solid knowledge. Pins:

- the scorer: blank → 0.0; unknown-marker → 0.3; plain LLM → 0.6; LLM
  corroborated by data (unit / enum values) → 0.8; source-comment → 0.9;
- human edits via the catalog PATCH endpoints land with
  ``description_source='human'`` and ``description_confidence=1.0``;
- the column PATCH endpoint updates writable fields and re-embeds.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import TimestampedBase
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta
from app.services.knowledge_graph.catalog_indexer import (
    score_description_confidence,
)


# ── scorer ────────────────────────────────────────────────────────────────


def test_blank_descriptions_score_zero():
    assert score_description_confidence("", None, "llm") == 0.0
    assert score_description_confidence(None, None, "") == 0.0


@pytest.mark.parametrize("marker", ["未知", "unknown", "Unknown"])
def test_unknown_marker_scores_low(marker):
    assert score_description_confidence(marker, marker, "llm") == 0.3


def test_plain_llm_description_scores_base():
    assert score_description_confidence("订单主表", "sales order header", "llm") == 0.6


def test_data_corroboration_raises_llm_confidence():
    assert score_description_confidence(
        "订单状态", "order status", "llm", enum_values=["A", "B"]
    ) == 0.8
    assert score_description_confidence(
        "金额", "amount", "llm", unit="元"
    ) == 0.8


def test_source_comment_scores_high():
    assert score_description_confidence("FCLOSESTATUS", "FCLOSESTATUS", "source_comment") == 0.9


def test_score_never_exceeds_nine_for_non_human():
    assert score_description_confidence(
        "状态", "status", "source_comment", unit="元", enum_values=["a", "b"]
    ) == 0.9


# ── human correction path ─────────────────────────────────────────────────


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TimestampedBase.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    session.add(KnowledgeBase(id="kb-1", name="w", source_kind="database"))
    session.flush()
    t = KBTableMeta(
        kb_id="kb-1", table_name="erp_t_sal_outstock",
        description_zh="出库表", description_en="outstock",
        description_source="llm", description_confidence=0.6,
    )
    session.add(t)
    session.flush()
    c = KBColumnMeta(
        table_meta_id=t.id, column_name="FQTY", ordinal=0, data_type="DECIMAL",
        description_zh="数量", description_en="qty",
        description_source="llm", description_confidence=0.6, unit=None,
    )
    session.add(c)
    session.commit()
    try:
        yield session, t, c
    finally:
        session.close()
        engine.dispose()


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _patch_table(db, table_id, body):
    from app.routers.knowledge_bases import update_catalog_table

    return _run(update_catalog_table(app_id="a", kb_id="kb-1", table_id=table_id, body=body, db=db))


def _patch_column(db, column_id, body):
    from app.routers.knowledge_bases import update_catalog_column

    return _run(update_catalog_column(app_id="a", kb_id="kb-1", column_id=column_id, body=body, db=db))


def test_table_edit_marks_human_verified(db):
    session, t, _c = db
    # Re-embed hits ChromaDB — neutralise it for the unit test.
    import app.routers.knowledge_bases as kb_router

    kb_router.update_table_embedding = lambda **kw: False
    resp = _patch_table(session, t.id, {"description_en": "sales outbound header"})
    assert resp["description_en"] == "sales outbound header"
    session.refresh(t)
    assert t.description_source == "human"
    assert t.description_confidence == 1.0


def test_column_edit_marks_human_verified(db):
    session, _t, c = db
    import app.routers.knowledge_bases as kb_router

    kb_router.update_column_embedding = lambda **kw: False
    resp = _patch_column(session, c.id, {"description_en": "quantity shipped", "unit": "PCS"})
    assert resp["description_en"] == "quantity shipped"
    session.refresh(c)
    assert c.description_source == "human"
    assert c.description_confidence == 1.0
    assert c.unit == "PCS"


def test_column_edit_rejects_unknown_fields(db):
    session, _t, c = db
    with pytest.raises(Exception):
        _patch_column(session, c.id, {"column_name": "hack"})


def test_column_edit_404_on_wrong_kb(db):
    session, _t, c = db
    from fastapi import HTTPException

    from app.routers.knowledge_bases import update_catalog_column

    import asyncio

    with pytest.raises(HTTPException):
        asyncio.run(update_catalog_column(
            app_id="a", kb_id="kb-other", column_id=c.id,
            body={"description_en": "x"}, db=session,
        ))
