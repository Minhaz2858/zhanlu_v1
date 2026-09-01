"""Tests for Terminology service — CRUD + semantic search."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.terminology import Terminology
from app.services.terminology.service import TerminologyService


@pytest.fixture
def term_db():
    """In-memory DB with the terminologies table."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng)
    s = Sess()
    yield s
    s.close()


class TestTerminologyCRUD:
    def test_upsert_creates_new_entry(self, term_db):
        svc = TerminologyService(term_db)
        entry = svc.upsert("ARR", "Annual Recurring Revenue", datasource_ids=["ds-1"])
        assert entry.id is not None
        assert entry.word == "ARR"
        assert entry.description == "Annual Recurring Revenue"
        assert "ds-1" in entry.datasource_ids

    def test_upsert_updates_existing(self, term_db):
        svc = TerminologyService(term_db)
        svc.upsert("ARR", "Annual Recurring Revenue", datasource_ids=["ds-1"])
        updated = svc.upsert("ARR", "Updated description", datasource_ids=["ds-1", "ds-2"])
        assert updated.description == "Updated description"
        # Should still be one row
        count = term_db.query(Terminology).filter(Terminology.word == "ARR").count()
        assert count == 1

    def test_list_for_datasource_filters_by_datasource(self, term_db):
        svc = TerminologyService(term_db)
        svc.upsert("MRR", "Monthly", datasource_ids=["ds-1"])
        svc.upsert("ARPU", "Avg per user", datasource_ids=["ds-2"])
        svc.upsert("Global term", "All datasources", datasource_ids=None)

        ds1 = svc.list_for_datasource("ds-1")
        ds1_words = {e.word for e in ds1}
        assert "MRR" in ds1_words
        assert "Global term" in ds1_words  # null datasource_ids means global
        assert "ARPU" not in ds1_words

    def test_search_by_word_returns_semantic_matches(self, term_db):
        svc = TerminologyService(term_db)
        svc.upsert("revenue", "Total income", datasource_ids=["ds-1"])
        svc.upsert("revenue forecast", "Projected future income", datasource_ids=["ds-1"])
        svc.upsert("headcount", "Number of employees", datasource_ids=["ds-1"])

        results = svc.search_by_word("income forecast", "ds-1", top_k=2)
        assert len(results) == 2
        words = [r[0] for r in results]
        # All 3 entries are in the index, top_k=2 returns 2 of them
        assert all(w in ("revenue", "revenue forecast", "headcount") for w in words)

    def test_search_by_word_empty_when_no_matches(self, term_db):
        svc = TerminologyService(term_db)
        results = svc.search_by_word("anything", "ds-1")
        assert results == []
