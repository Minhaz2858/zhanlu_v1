"""Tests for Q→SQL examples service — CRUD + semantic search."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.q_sql_example import QSqlExample
from app.services.q_sql_examples.service import QSqlExampleService


@pytest.fixture
def qsql_db():
    """In-memory DB with the q_sql_examples table."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng)
    s = Sess()
    yield s
    s.close()


class TestQSqlExampleCRUD:
    def test_add_creates_entry(self, qsql_db):
        svc = QSqlExampleService(qsql_db)
        entry = svc.add("Show total revenue", "SELECT SUM(amount) FROM orders", datasource_id="ds-1")
        assert entry.id is not None
        assert entry.question == "Show total revenue"
        assert entry.sql == "SELECT SUM(amount) FROM orders"
        assert entry.datasource_id == "ds-1"

    def test_list_for_datasource_filters_correctly(self, qsql_db):
        svc = QSqlExampleService(qsql_db)
        svc.add("Q1", "SELECT * FROM t1", datasource_id="ds-1")
        svc.add("Q2", "SELECT * FROM t2", datasource_id="ds-2")
        svc.add("Q3", "SELECT * FROM t3", datasource_id="ds-1")

        ds1 = svc.list_for_datasource("ds-1")
        assert len(ds1) == 2
        questions = {e.question for e in ds1}
        assert questions == {"Q1", "Q3"}

    def test_top_k_returns_semantic_matches(self, qsql_db):
        svc = QSqlExampleService(qsql_db)
        svc.add("Show me revenue by month", "SELECT month, SUM(amount) FROM orders GROUP BY month",
                datasource_id="ds-1")
        svc.add("List all customers", "SELECT * FROM customers",
                datasource_id="ds-1")
        svc.add("Revenue breakdown by product", "SELECT product, SUM(amount) FROM orders GROUP BY product",
                datasource_id="ds-1")

        results = svc.top_k("monthly income report", "ds-1", k=2)
        assert len(results) == 2
        # First result should be revenue-related
        question, sql, score = results[0]
        assert "revenue" in question.lower()

    def test_top_k_empty_when_no_examples(self, qsql_db):
        svc = QSqlExampleService(qsql_db)
        results = svc.top_k("anything", "ds-1")
        assert results == []
