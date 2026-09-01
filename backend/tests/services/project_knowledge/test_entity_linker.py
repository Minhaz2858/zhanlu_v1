"""Unit tests for entity_linker."""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models.knowledge_catalog import ProjectEntity, KBTableMeta
from app.services.project_knowledge.entity_linker import (
    seed_products_as_entities,
    link_entities_to_catalog_for_project,
    tokenize_for_match,
)


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


def test_seed_is_idempotent(db):
    settings.PROJECT_KNOWLEDGE_CACHE_ENABLED = True
    a = seed_products_as_entities(db, "p1")
    db.commit()
    b = seed_products_as_entities(db, "p1")
    db.commit()
    assert a == 13  # 13 products
    assert b == a  # no new rows
    count = db.query(ProjectEntity).filter_by(project_id="p1").count()
    assert count == a


def test_seed_skips_when_flag_off(db):
    settings.PROJECT_KNOWLEDGE_CACHE_ENABLED = False
    n = seed_products_as_entities(db, "p1")
    assert n == 0


def test_link_falls_back_to_name_match(db):
    settings.PROJECT_KNOWLEDGE_CACHE_ENABLED = True
    settings.PROJECT_KNOWLEDGE_LAYER_ENTITIES_ENABLED = True
    # create a fake KB + 1 table whose name matches an alias
    from app.models.knowledge_base import KnowledgeBase
    kb_id = str(uuid.uuid4())
    db.add(KnowledgeBase(
        id=kb_id, name="wh", source_kind="db", db_type="mysql",
        host="h", port=3306, database_name="d", username="u", password="p",
    ))
    db.commit()
    db.add(KBTableMeta(
        id=str(uuid.uuid4()),
        kb_id=kb_id, schema_name="public", table_name="md_t_lz_isoprene_price",
        table_type="TABLE", table_role="unknown",
    ))
    db.commit()
    seed_products_as_entities(db, "p1")
    db.commit()
    # Patch the embedding client to fail so we force name-match
    with patch("app.services.project_knowledge.entity_linker._try_embedding_link",
               return_value=0):
        n = link_entities_to_catalog_for_project(db, "p1", kb_id)
    assert n >= 1


def test_tokenize_ascii():
    toks = tokenize_for_match("isoprene price")
    assert "isoprene" in toks
    assert "price" in toks


def test_tokenize_mixed():
    toks = tokenize_for_match("异戊二烯 isoprene price")
    assert "isoprene" in toks
    assert any("异戊二" in t for t in toks)