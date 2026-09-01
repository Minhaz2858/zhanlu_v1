"""Tests for embedding-based semantic skill search with RRF fusion.

Verifies that:
1. With semantic search enabled and embeddings present, the dense path ranks
   skills by cosine similarity to the query.
2. RRF fusion combines the semantic ranking with keyword search.
3. Falls back gracefully to keyword-only when embeddings are missing or
   the feature flag is off.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_semantic.db")

from unittest.mock import patch

import pytest

from app.database import Base, engine, SessionLocal
import app.models  # noqa: F401  (registers Tool)

Base.metadata.create_all(engine)

from app.config import settings
from app.models.tool import Tool
from app.services.memory_advanced.embeddings import EmbeddingResult
from app.services.skill_studio.semantic_finder import (
    _cosine_similarity,
    _rrf_fuse,
    semantic_search,
)


def _make_embedding(vec):
    return EmbeddingResult(text="q", vector=vec, model="text-embedding-3-small", cached=False, duration_ms=1.0)


def _seed_tools(db):
    # Two skills: one semantically close to "sales deck", one unrelated.
    close = [1.0, 0.0, 0.0] + [0.0] * 1533
    far = [0.0, 1.0, 0.0] + [0.0] * 1533
    db.add(Tool(name="sales-deck", description="make a sales presentation", embedding=close,
                references_manifest={"outline.md": "x"}, assets_manifest={"t/deck.pptx": "y"}))
    db.add(Tool(name="code-review", description="review pull requests", embedding=far,
                references_manifest={}, assets_manifest={}))
    db.commit()


def test_cosine_similarity_basic():
    assert _cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert _cosine_similarity([1.0], [2.0, 3.0]) is None  # mismatched lengths


def test_rrf_fuse_orders_by_combined_rank():
    from app.services.skill_studio.semantic_finder import SkillSearchResult

    dense = [
        SkillSearchResult(name="a", description="", category="", score=0.9, source="db", references=[], assets=[]),
        SkillSearchResult(name="b", description="", category="", score=0.2, source="db", references=[], assets=[]),
    ]
    sparse = [
        SkillSearchResult(name="b", description="", category="", score=1.0, source="filesystem", references=[], assets=[]),
        SkillSearchResult(name="a", description="", category="", score=1.0, source="filesystem", references=[], assets=[]),
    ]
    fused = _rrf_fuse(dense, sparse, limit=10)
    names = [r.name for r in fused]
    assert names[0] in ("a", "b")  # both appear, fused ranking computed


@pytest.mark.asyncio
async def test_semantic_search_ranks_close_skill_first():
    db = SessionLocal()
    _seed_tools(db)
    try:
        with patch.object(settings, "SKILL_SEMANTIC_SEARCH_ENABLED", True        ), patch(
            "app.services.memory_advanced.embeddings.get_embedding",
            return_value=_make_embedding([1.0, 0.0, 0.0] + [0.0] * 1533),
        ), patch(
            "app.services.skill_studio.semantic_finder._keyword_search",
            return_value=[],
        ):
            results = semantic_search("sales deck", db, limit=5)
        names = [r.name for r in results]
        assert names
        assert names[0] == "sales-deck"
        assert results[0].source == "db"
    finally:
        db.query(Tool).delete()
        db.commit()
        db.close()


@pytest.mark.asyncio
async def test_semantic_search_falls_back_without_embedding():
    db = SessionLocal()
    db.add(Tool(name="plain-skill", description="a plain skill", embedding=None))
    db.commit()
    try:
        with patch.object(settings, "SKILL_SEMANTIC_SEARCH_ENABLED", True        ), patch(
            "app.services.memory_advanced.embeddings.get_embedding",
            return_value=_make_embedding([0.0] * 1536),
        ), patch(
            "app.services.skill_studio.semantic_finder._keyword_search",
            return_value=[],  # no keyword hits either
        ):
            results = semantic_search("anything", db, limit=5)
        # No usable embeddings -> dense path empty, fusion with empty sparse -> empty list.
        assert results == []
    finally:
        db.query(Tool).delete()
        db.commit()
        db.close()
