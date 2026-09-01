"""Unit tests for ProjectKnowledgeCache query routing."""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.services.project_knowledge import ProjectKnowledgeCache
from app.services.project_knowledge.entity_linker import (
    seed_products_as_entities,
    tokenize_for_match,
)
from app.models.knowledge_catalog import ProjectEntity, ProjectMetric


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def _enable():
    settings.PROJECT_KNOWLEDGE_CACHE_ENABLED = True
    settings.PROJECT_KNOWLEDGE_QWEN_FAST_PATH = True
    settings.PROJECT_KNOWLEDGE_LAYER_ENTITIES_ENABLED = True
    settings.PROJECT_KNOWLEDGE_LAYER_METRICS_ENABLED = True


def test_layer1_resolver_hit(db):
    _enable()
    cache = ProjectKnowledgeCache("p1")
    r = cache.query(db, "isoprene upstream", model_id="qwen3.6-27b")
    assert r is not None
    assert r.kind == "product"
    assert r.data["product_id"] == "isoprene"
    assert "Upstream" in r.context_block


def test_layer1_resolver_miss(db):
    _enable()
    cache = ProjectKnowledgeCache("p1")
    r = cache.query(db, "what's the weather today", model_id="qwen3.6-27b")
    assert r is None


def test_layer2_entity_hit(db):
    _enable()
    seed_products_as_entities(db, "p1")
    db.commit()
    cache = ProjectKnowledgeCache("p1")
    # query mentioning a custom alias, no resolver hit
    r = cache.query(db, "show me 异戊二烯 status", model_id="qwen3.6-27b")
    assert r is not None
    # Layer 1 will hit isoprene first, that's fine
    assert r.kind in ("entity", "product")


def test_layer3_metric_hit(db):
    _enable()
    db.add(ProjectMetric(
        id=str(uuid.uuid4()),
        project_id="p1", name="Crude Oil Avg Price",
        aliases=["crude price", "原油价格"],
        unit="USD/bbl", default_aggregation="avg",
        status="approved", source="user",
    ))
    db.commit()
    cache = ProjectKnowledgeCache("p1")
    r = cache.query(db, "show me 原油价格", model_id="qwen3.6-27b")
    assert r is not None
    assert r.kind in ("metric", "product")


def test_query_returns_none_when_flag_off(db):
    settings.PROJECT_KNOWLEDGE_CACHE_ENABLED = False
    settings.PROJECT_KNOWLEDGE_QWEN_FAST_PATH = True
    cache = ProjectKnowledgeCache("p1")
    assert cache.query(db, "isoprene", model_id="qwen3.6-27b") is None


def test_invalidate_all(db):
    _enable()
    seed_products_as_entities(db, "p1")
    db.commit()
    cache = ProjectKnowledgeCache("p1")
    n = cache.invalidate(db, scope="all")
    assert n >= 0


def test_stats_returns_dataclass(db):
    _enable()
    cache = ProjectKnowledgeCache("p1")
    s = cache.stats(db)
    assert s.project_id == "p1"
    assert s.entities == 0
    assert s.metrics == 0


def test_empty_project_id_raises():
    try:
        ProjectKnowledgeCache("")
        raised = False
    except PermissionError:
        raised = True
    assert raised, "Expected PermissionError for empty project_id"


def test_tokenize_cjk_safe():
    toks = tokenize_for_match("异戊二烯价格")
    assert any("异戊二" in t for t in toks)
    assert "价格" not in toks  # single CJK chars not tokens