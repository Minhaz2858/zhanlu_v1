"""Entity Extractor — generic entity extraction from project memory.

Tests cover: LLM extraction shape, idempotency via source_ref, confidence
threshold for links, and the hard constraint that the extraction prompt
contains NO domain-specific examples.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.config import settings
from app.database import Base
from app.models.knowledge_catalog import ProjectEntity, ProjectEntityLink
from app.models.project_memory import ProjectMemory
from app.services.knowledge_graph import entity_extractor as ee


@pytest.fixture
def db(tmp_path):
    db_file = tmp_path / f"test_{uuid.uuid4().hex[:8]}.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    original = getattr(settings, "ENTITY_GRAPH_ENABLED", False)
    settings.ENTITY_GRAPH_ENABLED = True
    try:
        yield s
    finally:
        settings.ENTITY_GRAPH_ENABLED = original
        s.close()
        engine.dispose()


PID = "proj-1"


def _memory(db, content="We decided to track monthly revenue for Acme Corp.", entry_type="decision"):
    m = ProjectMemory(
        id=str(uuid.uuid4()), project_id=PID, entry_type=entry_type,
        content=content, content_hash="hash123",
        org_id="default-org", app_id="default-app",
    )
    db.add(m)
    db.commit()
    return m


class TestPromptGeneric:
    """The extraction prompt must contain ZERO domain-specific examples."""

    def test_prompt_has_no_domain_examples(self):
        full = ee._EXTRACTION_SYSTEM + ee._build_extraction_prompt("dummy text")
        # Must NOT contain any petrochemical / domain-specific terms
        forbidden = ["C5", "C9", "ethylene", "naphtha", "cracked", "裂解", "乙烯", "石脑油"]
        for term in forbidden:
            assert term.lower() not in full.lower(), (
                f"Extraction prompt contains domain-specific term: {term}"
            )

    def test_prompt_lists_universal_types(self):
        full = ee._EXTRACTION_SYSTEM + ee._build_extraction_prompt("dummy")
        for t in ("product", "customer", "metric", "concept", "organization", "location"):
            assert t in full


class TestExtraction:
    def test_extracts_entities_from_memory(self, db):
        mem = _memory(db, "We track monthly revenue for Acme Corp. and monitor thesupplier BigChem Ltd.")
        mock_llm = AsyncMock(return_value={
            "data": {
                "entities": [
                    {"name": "Monthly Revenue", "entity_type": "metric",
                     "aliases": ["revenue"], "description": "tracked monthly"},
                    {"name": "Acme Corp.", "entity_type": "customer",
                     "aliases": ["Acme"], "description": "a customer"},
                    {"name": "BigChem Ltd.", "entity_type": "organization",
                     "aliases": ["BigChem"], "description": "a supplier"},
                ]
            }
        })
        with patch.object(ee, "call_llm", mock_llm):
            entities = ee.extract_entities_sync(db, mem)

        db.commit()
        assert len(entities) == 3
        types = {e.entity_type for e in entities}
        assert types == {"metric", "customer", "organization"}
        # All have source_ref set to the memory id
        assert all(e.source_ref == mem.id for e in entities)

    def test_invalid_entity_type_defaults_to_concept(self, db):
        mem = _memory(db, "some text")
        mock_llm = AsyncMock(return_value={
            "data": {"entities": [
                {"name": "X", "entity_type": "weird_type", "description": ""},
            ]}
        })
        with patch.object(ee, "call_llm", mock_llm):
            entities = ee.extract_entities_sync(db, mem)
        db.commit()
        assert entities[0].entity_type == "concept"

    def test_empty_extraction_returns_empty(self, db):
        mem = _memory(db, "hello world")
        mock_llm = AsyncMock(return_value={"data": {"entities": []}})
        with patch.object(ee, "call_llm", mock_llm):
            entities = ee.extract_entities_sync(db, mem)
        assert entities == []

    def test_llm_failure_returns_empty_no_raise(self, db):
        mem = _memory(db, "hello")
        mock_llm = AsyncMock(side_effect=RuntimeError("LLM down"))
        with patch.object(ee, "call_llm", mock_llm):
            entities = ee.extract_entities_sync(db, mem)
        assert entities == []


class TestIdempotency:
    def test_same_memory_processed_twice_no_duplicates(self, db):
        mem = _memory(db, "Track revenue for Acme.")
        mock_llm = AsyncMock(return_value={
            "data": {"entities": [
                {"name": "Revenue", "entity_type": "metric", "description": ""},
                {"name": "Acme", "entity_type": "customer", "description": ""},
            ]}
        })
        with patch.object(ee, "call_llm", mock_llm):
            ee.extract_entities_sync(db, mem)
            db.commit()
            # Second call — should skip entirely
            entities2 = ee.extract_entities_sync(db, mem)
        db.commit()
        assert entities2 == []
        assert db.query(ProjectEntity).filter_by(project_id=PID).count() == 2

    def test_different_memories_produce_different_entities(self, db):
        m1 = _memory(db, "Track revenue for Acme.")
        m2 = ProjectMemory(
            id=str(uuid.uuid4()), project_id=PID, entry_type="fact",
            content="Globex is a new customer.", content_hash="h2",
            org_id="default-org", app_id="default-app",
        )
        db.add(m2)
        db.commit()
        mock_llm = AsyncMock(return_value={
            "data": {"entities": [
                {"name": "Revenue", "entity_type": "metric", "description": ""},
            ]}
        })
        with patch.object(ee, "call_llm", mock_llm):
            ee.extract_entities_sync(db, m1)
            db.commit()
        mock_llm2 = AsyncMock(return_value={
            "data": {"entities": [
                {"name": "Globex", "entity_type": "customer", "description": ""},
            ]}
        })
        with patch.object(ee, "call_llm", mock_llm2):
            ee.extract_entities_sync(db, m2)
            db.commit()
        assert db.query(ProjectEntity).filter_by(project_id=PID).count() == 2


class TestEntityUpsert:
    def test_duplicate_name_upserts_not_inserts(self, db):
        """Same entity name from different memories → one row, updated."""
        m1 = _memory(db, "Revenue is important.")
        mock_llm = AsyncMock(return_value={
            "data": {"entities": [
                {"name": "Revenue", "entity_type": "metric", "description": "v1"},
            ]}
        })
        with patch.object(ee, "call_llm", mock_llm):
            ee.extract_entities_sync(db, m1)
            db.commit()

        # Simulate a second memory mentioning same entity — but with a
        # different source_ref so it's not skipped
        m2 = ProjectMemory(
            id=str(uuid.uuid4()), project_id=PID, entry_type="fact",
            content="Revenue grew.", content_hash="h2",
            org_id="default-org", app_id="default-app",
        )
        db.add(m2)
        db.commit()
        mock_llm2 = AsyncMock(return_value={
            "data": {"entities": [
                {"name": "Revenue", "entity_type": "metric", "description": "v2",
                 "aliases": ["rev"]},
            ]}
        })
        with patch.object(ee, "call_llm", mock_llm2):
            ee.extract_entities_sync(db, m2)
            db.commit()

        rows = db.query(ProjectEntity).filter_by(project_id=PID, name="Revenue").all()
        assert len(rows) == 1
        assert rows[0].description == "v2"
        assert rows[0].aliases == ["rev"]
