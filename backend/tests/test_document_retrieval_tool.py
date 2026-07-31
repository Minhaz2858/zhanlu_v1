"""Tests for the document retrieval tools (search_documents, answer_from_documents).

These test the tool-handler-level validation (kb_id required, bound-set
enforcement, wrong source_kind rejection) without loading the real
embedding model or calling the LLM.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import TimestampedBase
from app.models.knowledge_base import KnowledgeBase


@pytest.mark.asyncio
async def test_search_documents_requires_kb_id():
    from app.services.tool_handlers import db_tools
    res = await db_tools._search_documents({}, db=None, user_id=None, context={})
    assert res["success"] is False
    assert "data_source_id" in res["error"]


@pytest.mark.asyncio
async def test_search_documents_unbound_kb_rejected():
    from app.services.tool_handlers import db_tools
    res = await db_tools._search_documents(
        {"data_source_id": "kb-x", "query": "foo"},
        db=None, user_id=None,
        context={"bound_kb_ids": ["kb-other"]},
    )
    assert res["success"] is False
    assert "not bound" in res["error"]


@pytest.mark.asyncio
async def test_search_documents_wrong_source_kind_rejected():
    """A database-kind KB should be rejected by the doc tool."""
    from app.services.tool_handlers import db_tools

    engine = create_engine("sqlite:///:memory:")
    TimestampedBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        kb = KnowledgeBase(
            name="db1", source_kind="database", db_type="postgresql",
            org_id="o", app_id="a",
        )
        db.add(kb); db.commit(); db.refresh(kb)
        res = await db_tools._search_documents(
            {"data_source_id": kb.id, "query": "foo"},
            db=db, user_id=None,
            context={"bound_kb_ids": [kb.id]},
        )
        assert res["success"] is False
        assert "source_kind" in res["error"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_search_documents_index_not_ready():
    """A file KB that hasn't been indexed should return a clear error."""
    from app.services.tool_handlers import db_tools

    engine = create_engine("sqlite:///:memory:")
    TimestampedBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        kb = KnowledgeBase(
            name="doc1", source_kind="file", file_type="txt",
            file_url="/api/uploads/x.txt",
            indexing_status=None,  # not indexed
            org_id="o", app_id="a",
        )
        db.add(kb); db.commit(); db.refresh(kb)
        res = await db_tools._search_documents(
            {"data_source_id": kb.id, "query": "foo"},
            db=db, user_id=None,
            context={"bound_kb_ids": [kb.id]},
        )
        assert res["success"] is False
        assert "not ready" in res["error"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_answer_from_documents_requires_question():
    from app.services.tool_handlers import db_tools
    res = await db_tools._answer_from_documents(
        {"data_source_id": "kb-x"},
        db=None, user_id=None,
        context={"bound_kb_ids": ["kb-x"]},
    )
    assert res["success"] is False
    assert "question" in res["error"]
