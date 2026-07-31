"""Contract tests for SQLite and Postgres datasource adapters."""

import os
import pytest
from pathlib import Path

from app.services.datasources import DatasourceAdapter
from app.services.datasources.sqlite_adapter import SQLiteAdapter


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
