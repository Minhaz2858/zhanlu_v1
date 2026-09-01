"""Contract tests for SQLite and Postgres datasource adapters."""

import os
import pytest
from pathlib import Path
from types import SimpleNamespace

from app.services.datasources import DatasourceAdapter, build_adapter
from app.services.datasources.sqlite_adapter import SQLiteAdapter


def _kb(**overrides):
    """Build a fake KnowledgeBase-shaped object for dispatch tests."""
    base = dict(
        id="kb-1",
        name="test",
        db_type="postgres",
        host="localhost",
        port=5432,
        database_name="zhanlu",
        schema=None,
        username="zhanlu",
        password="secret",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def sqlite_adapter():
    """In-memory SQLite adapter with sample schema."""
    import sqlite3
    adapter = SQLiteAdapter(db_path=":memory:")
    conn = adapter._get_conn()
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            amount REAL NOT NULL,
            status TEXT DEFAULT 'pending'
        );
        INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'alice@test.com');
        INSERT INTO users (id, name, email) VALUES (2, 'Bob', 'bob@test.com');
        INSERT INTO orders (id, user_id, amount, status) VALUES (1, 1, 99.99, 'done');
        INSERT INTO orders (id, user_id, amount, status) VALUES (2, 2, 49.50, 'pending');
    """)
    conn.commit()
    return adapter


class TestSQLiteAdapter:
    def test_test_connection(self, sqlite_adapter):
        assert sqlite_adapter.test_connection() is True

    def test_list_tables(self, sqlite_adapter):
        tables = sqlite_adapter.list_tables()
        assert "users" in tables
        assert "orders" in tables

    def test_describe_users_table(self, sqlite_adapter):
        cols = sqlite_adapter.describe_table("users")
        names = [c.name for c in cols]
        assert "id" in names
        assert "name" in names
        assert "email" in names

        id_col = next(c for c in cols if c.name == "id")
        assert id_col.is_pk is True

    def test_refresh_schema(self, sqlite_adapter):
        schema = sqlite_adapter.refresh_schema()
        assert "users" in schema
        assert "orders" in schema
        assert len(schema["users"]) == 4
        assert len(schema["orders"]) == 4

    def test_explain_select(self, sqlite_adapter):
        result = sqlite_adapter.explain("SELECT * FROM users WHERE name = 'Alice'")
        assert len(result.plan_text) > 0
        assert result.estimated_cost >= 0

    def test_query_select_all(self, sqlite_adapter):
        result = sqlite_adapter.query("SELECT * FROM users", row_limit=10)
        assert result.row_count == 2
        assert result.columns == ["id", "name", "email", "created_at"]
        assert len(result.rows) == 2

    def test_query_respects_row_limit(self, sqlite_adapter):
        result = sqlite_adapter.query("SELECT * FROM users", row_limit=1)
        assert result.row_count == 1

    def test_query_with_join(self, sqlite_adapter):
        result = sqlite_adapter.query(
            "SELECT u.name, o.amount FROM users u JOIN orders o ON u.id = o.user_id"
        )
        assert result.row_count == 2

    def test_connection_broken_db(self):
        adapter = SQLiteAdapter(db_path="/nonexistent/dir/db.sqlite")
        assert adapter.test_connection() is False

    def test_close_and_reopen(self, sqlite_adapter):
        sqlite_adapter.close()
        assert sqlite_adapter._conn is None
        assert sqlite_adapter.test_connection() is True


class TestPostgresAdapter:
    """Skip unless PGTEST_DSN env var is set."""

    @pytest.fixture
    def pg_dsn(self):
        dsn = os.environ.get("PGTEST_DSN", "")
        if not dsn:
            pytest.skip("PGTEST_DSN not set — skipping Postgres adapter tests")
        return dsn

    @pytest.fixture
    def pg_adapter(self, pg_dsn):
        from app.services.datasources.postgres_adapter import PostgresAdapter
        return PostgresAdapter(dsn=pg_dsn)

    def test_test_connection(self, pg_adapter):
        assert pg_adapter.test_connection() is True

    def test_list_tables_non_empty(self, pg_adapter):
        tables = pg_adapter.list_tables()
        assert isinstance(tables, list)


class TestBuildAdapterDispatch:
    """build_adapter must select the correct adapter by db_type (no DB I/O)."""

    def test_mysql_dispatches_to_mysql_adapter(self):
        from app.services.datasources.mysql_adapter import MySQLAdapter
        adapter = build_adapter(_kb(db_type="mysql"))
        assert isinstance(adapter, MySQLAdapter)

    def test_mariadb_dispatches_to_mysql_adapter(self):
        from app.services.datasources.mysql_adapter import MySQLAdapter
        adapter = build_adapter(_kb(db_type="mariadb"))
        assert isinstance(adapter, MySQLAdapter)

    def test_postgres_dispatches_to_postgres_adapter(self):
        from app.services.datasources.postgres_adapter import PostgresAdapter
        adapter = build_adapter(_kb(db_type="postgres"))
        assert isinstance(adapter, PostgresAdapter)

    def test_postgresql_alias_dispatches_to_postgres_adapter(self):
        from app.services.datasources.postgres_adapter import PostgresAdapter
        adapter = build_adapter(_kb(db_type="postgresql"))
        assert isinstance(adapter, PostgresAdapter)

    def test_postgres_schema_is_threaded(self):
        from app.services.datasources.postgres_adapter import PostgresAdapter
        adapter = build_adapter(_kb(db_type="postgres", schema="analytics"))
        assert isinstance(adapter, PostgresAdapter)
        assert adapter._schema == "analytics"

    def test_sqlite_dispatches_to_sqlite_adapter(self):
        adapter = build_adapter(_kb(db_type="sqlite", database_name=":memory:"))
        assert isinstance(adapter, SQLiteAdapter)

    def test_unknown_db_type_raises_value_error(self):
        with pytest.raises(ValueError):
            build_adapter(_kb(db_type="oracle"))

    def test_empty_db_type_raises_value_error(self):
        with pytest.raises(ValueError):
            build_adapter(_kb(db_type=None))


class TestPostgresSchemaFiltering:
    """PostgresAdapter/PostgresConnector must scope introspection by schema."""

    def test_adapter_defaults_to_public(self):
        from app.services.datasources.postgres_adapter import PostgresAdapter
        adapter = PostgresAdapter(host="localhost")
        assert adapter._schema == "public"

    def test_adapter_stores_non_default_schema(self):
        from app.services.datasources.postgres_adapter import PostgresAdapter
        adapter = PostgresAdapter(host="localhost", schema="dw_prod")
        assert adapter._schema == "dw_prod"

    def test_adapter_schema_blank_falls_back_to_public(self):
        from app.services.datasources.postgres_adapter import PostgresAdapter
        adapter = PostgresAdapter(host="localhost", schema="")
        assert adapter._schema == "public"

    def test_connector_search_path_defaults_to_none(self):
        from app.services.db.postgres import PostgresConnector
        conn = PostgresConnector(_kb(db_type="postgres", schema=None))
        assert conn._search_path_option() is None

    def test_connector_search_path_for_public_is_none(self):
        from app.services.db.postgres import PostgresConnector
        conn = PostgresConnector(_kb(db_type="postgres", schema="public"))
        assert conn._search_path_option() is None

    def test_connector_search_path_for_custom_schema(self):
        from app.services.db.postgres import PostgresConnector
        conn = PostgresConnector(_kb(db_type="postgres", schema="dw_prod"))
        assert conn._search_path_option() == '-csearch_path="dw_prod"'

    def test_connector_search_path_escapes_quotes(self):
        from app.services.db.postgres import PostgresConnector
        conn = PostgresConnector(_kb(db_type="postgres", schema='a"b'))
        assert conn._search_path_option() == '-csearch_path="a""b"'
