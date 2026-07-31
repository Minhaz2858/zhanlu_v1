"""Tests for ContextBuilder — assembles the full LLM prompt from all metadata sources."""

import os
import sqlite3
import tempfile
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.datasource import Datasource
from app.models.agent_data_binding import AgentDataBinding


@pytest.fixture
def ctx_db():
    """In-memory DB with all tables."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng)
    s = Sess()
    yield s
    s.close()


@pytest.fixture
def ctx_sqlite_path():
    """Temporary SQLite DB with test data."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    con = sqlite3.connect(tmp.name)
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, amount REAL)")
    con.execute("INSERT INTO orders VALUES (1,'EU',100),(2,'US',50)")
    con.commit()
    con.close()
    yield tmp.name
    os.unlink(tmp.name)


class TestContextBuilder:
    def test_build_includes_m_schema(self, ctx_db, ctx_sqlite_path):
        from app.services.nl2sql.context_builder import ContextBuilder

        binding = {
            "datasource_id": "ds-ctx-1",
            "allowed_tables": ["orders"],
            "agent_app_id": "a-ctx-1",
        }
        ds_config = {"path": ctx_sqlite_path, "dialect": "sqlite"}

        builder = ContextBuilder(ctx_db)
        result = builder.build("show orders", binding, ds_config)

        assert "Table:" in result or "orders" in result.lower()
        assert "region" in result.lower()

    def test_build_includes_dialect_rules(self, ctx_db, ctx_sqlite_path):
        from app.services.nl2sql.context_builder import ContextBuilder

        binding = {
            "datasource_id": "ds-ctx-2",
            "allowed_tables": ["orders"],
            "agent_app_id": "a-ctx-2",
        }
        ds_config = {"path": ctx_sqlite_path, "dialect": "sqlite"}

        builder = ContextBuilder(ctx_db)
        result = builder.build("show orders", binding, ds_config)

        assert "<dialect-rules>" in result

    def test_build_includes_terminology_when_present(self, ctx_db, ctx_sqlite_path):
        from app.services.nl2sql.context_builder import ContextBuilder
        from app.models.terminology import Terminology

        term = Terminology(word="ARR", description="Annual Recurring Revenue",
                           datasource_ids='["ds-ctx-3"]', enabled=True)
        ctx_db.add(term)
        ctx_db.commit()

        binding = {
            "datasource_id": "ds-ctx-3",
            "allowed_tables": ["orders"],
            "agent_app_id": "a-ctx-3",
        }
        ds_config = {"path": ctx_sqlite_path, "dialect": "sqlite"}

        builder = ContextBuilder(ctx_db)
        result = builder.build("what is ARR", binding, ds_config)

        assert "ARR" in result
        assert "Annual Recurring Revenue" in result

    def test_build_includes_q_sql_examples_when_present(self, ctx_db, ctx_sqlite_path):
        from app.services.nl2sql.context_builder import ContextBuilder
        from app.models.q_sql_example import QSqlExample

        ex = QSqlExample(
            question="Show revenue by region",
            sql="SELECT region, SUM(amount) FROM orders GROUP BY region",
            datasource_id="ds-ctx-4",
            enabled=True,
        )
        ctx_db.add(ex)
        ctx_db.commit()

        binding = {
            "datasource_id": "ds-ctx-4",
            "allowed_tables": ["orders"],
            "agent_app_id": "a-ctx-4",
        }
        ds_config = {"path": ctx_sqlite_path, "dialect": "sqlite"}

        builder = ContextBuilder(ctx_db)
        result = builder.build("revenue report", binding, ds_config)

        assert "Show revenue by region" in result
        assert "SUM(amount)" in result

    def test_build_handles_empty_db_gracefully(self, ctx_db, ctx_sqlite_path):
        from app.services.nl2sql.context_builder import ContextBuilder

        binding = {
            "datasource_id": "ds-empty",
            "allowed_tables": ["orders"],
            "agent_app_id": "a-empty",
        }
        ds_config = {"path": ctx_sqlite_path, "dialect": "sqlite"}

        builder = ContextBuilder(ctx_db)
        result = builder.build("anything", binding, ds_config)

        # Should still contain m-schema and dialect rules
        assert "<dialect-rules>" in result
        assert "Table:" in result or "orders" in result.lower()
