"""Tests that the cache is strictly project-scoped.

Two projects: P1 (the 'ecisco_bi_assistant' AgentApp is bound to it),
P2 (a different AgentApp). Cache writes/reads for P1 must not appear
under P2 and vice versa.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models.knowledge_catalog import ProjectEntity
from app.services.project_knowledge import ProjectKnowledgeCache
from app.services.project_knowledge.entity_linker import seed_products_as_entities


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


def test_seed_isolates_projects(db):
    settings.PROJECT_KNOWLEDGE_CACHE_ENABLED = True
    seed_products_as_entities(db, "p_ecisco")
    seed_products_as_entities(db, "p_other")
    db.commit()
    ecisco_count = db.query(ProjectEntity).filter_by(project_id="p_ecisco").count()
    other_count = db.query(ProjectEntity).filter_by(project_id="p_other").count()
    assert ecisco_count == other_count == 13


def test_invalidate_only_target_project(db):
    settings.PROJECT_KNOWLEDGE_CACHE_ENABLED = True
    seed_products_as_entities(db, "p_ecisco")
    seed_products_as_entities(db, "p_other")
    db.commit()
    ProjectKnowledgeCache("p_ecisco").invalidate(db, scope="all")
    ecisco_after = db.query(ProjectEntity).filter_by(
        project_id="p_ecisco", is_deleted=False,
    ).count()
    other_after = db.query(ProjectEntity).filter_by(
        project_id="p_other", is_deleted=False,
    ).count()
    assert ecisco_after == 0
    assert other_after == 13


def test_stats_isolates_projects(db):
    settings.PROJECT_KNOWLEDGE_CACHE_ENABLED = True
    seed_products_as_entities(db, "p_ecisco")
    seed_products_as_entities(db, "p_other")
    db.commit()
    s1 = ProjectKnowledgeCache("p_ecisco").stats(db)
    s2 = ProjectKnowledgeCache("p_other").stats(db)
    assert s1.entities == 13
    assert s2.entities == 13
    assert s1.project_id == "p_ecisco"
    assert s2.project_id == "p_other"


def test_query_layer2_respects_project_scope(db):
    """When Layer 1 misses, Layer 2 should ONLY return entities from
    the same project_id, never from another project."""
    settings.PROJECT_KNOWLEDGE_CACHE_ENABLED = True
    settings.PROJECT_KNOWLEDGE_LAYER_ENTITIES_ENABLED = True
    # seed both projects
    seed_products_as_entities(db, "p_ecisco")
    seed_products_as_entities(db, "p_other")
    db.commit()
    # query with no resolver hit (something custom that no alias matches)
    cache_p2 = ProjectKnowledgeCache("p_other")
    # craft a token that matches BOTH projects' seed alias
    r = cache_p2._layer2_entities(db, "show me 异戊二烯")
    # if hit, every entity must belong to p_other
    if r is not None and r.kind == "entity":
        for ent in r.data["entities"]:
            # name/aliases can be from c5c9_seed but the entity row must
            # only be in p_other scope (Layer 2 query already filtered
            # by project_id=self.project_id, so this is the test)
            pass  # coverage by query WHERE clause
    # explicit: even if the question contains a generic c5/c9 token, the
    # returned entity IDs must be from p_other's ProjectEntity rows
    if r is not None and r.kind == "entity":
        all_p_other_ids = {
            e.id for e in db.query(ProjectEntity).filter(
                ProjectEntity.project_id == "p_other",
                ProjectEntity.is_deleted == False,  # noqa: E712
            ).all()
        }
        for ent in r.data["entities"]:
            assert ent["id"] in all_p_other_ids, f"entity {ent['id']} leaked from another project"