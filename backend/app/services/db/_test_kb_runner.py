"""Lightweight test runner for the DB services module.

Runs the same test cases as `app/services/db/_test_kb.py` but using
stdlib `unittest` (no pytest dependency). Invoke:

    /root/zhanlu/backend/venv/bin/python -m app.services.db._test_kb_runner
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import types
import unittest

# Make `app` importable when running this file directly.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


def _make_kb_stub(path: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id="kb_test",
        name="Test KB",
        db_type="sqlite",
        source_kind="database",
        host=None,
        port=None,
        database_name=None,
        username=None,
        password=None,
        api_url=path,
        file_url=path,
    )


class _SqliteKB(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        con = sqlite3.connect(self.path)
        try:
            con.executescript(
                """
                CREATE TABLE customers (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    region TEXT
                );
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY,
                    customer_id INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    created_at TEXT
                );
                INSERT INTO customers (id, name, region) VALUES
                  (1, 'Alice', 'EU'),
                  (2, 'Bob',   'EU'),
                  (3, 'Carol', 'US');
                INSERT INTO orders (id, customer_id, amount, created_at) VALUES
                  (1, 1, 100.0, '2026-01-15'),
                  (2, 1, 250.0, '2026-02-20'),
                  (3, 2, 75.0,  '2026-03-05'),
                  (4, 3, 600.0, '2026-04-10');
                """
            )
            con.commit()
        finally:
            con.close()
        self.kb = _make_kb_stub(self.path)

    def tearDown(self) -> None:
        try:
            os.unlink(self.path)
        except OSError:
            pass

    # --- factory + util --------------------------------------------------

    def test_supported_db_types(self):
        from app.services.db.connector_factory import supported_db_types
        types = supported_db_types()
        for t in ("sqlite", "mysql", "postgres", "mssql", "oracle"):
            self.assertIn(t, types)

    def test_factory_unknown_db_type_raises(self):
        from app.services.db.connector_factory import get_connector
        with self.assertRaises(ValueError):
            get_connector(types.SimpleNamespace(db_type="mongodb", id="x"))

    def test_factory_missing_db_type_raises(self):
        from app.services.db.connector_factory import get_connector
        with self.assertRaises(ValueError):
            get_connector(types.SimpleNamespace(db_type=None, id="x"))

    def test_quote_ident_dialects(self):
        from app.services.db.base import quote_ident
        self.assertEqual(quote_ident("users", "mysql"), "`users`")
        self.assertEqual(quote_ident("users", "postgres"), '"users"')
        self.assertEqual(quote_ident("users", "mssql"), "[users]")
        self.assertEqual(quote_ident("users", "sqlite"), '"users"')
        self.assertEqual(quote_ident("users", "oracle"), '"users"')

    def test_quote_ident_rejects_unsafe(self):
        from app.services.db.base import quote_ident
        for bad in ("1abc", "drop table", "a;b", "a--b", ""):
            with self.assertRaises(ValueError):
                quote_ident(bad, "postgres")

    # --- sqlite connector ------------------------------------------------

    def test_sqlite_list_tables(self):
        from app.services.db.sqlite import SQLiteConnector
        with SQLiteConnector(self.kb) as conn:
            tables = conn.list_tables()
        self.assertEqual(set(tables), {"customers", "orders"})

    def test_sqlite_describe_table(self):
        from app.services.db.sqlite import SQLiteConnector
        with SQLiteConnector(self.kb) as conn:
            cols = conn.describe_table("customers")
        by_name = {c["name"]: c for c in cols}
        self.assertTrue(by_name["id"]["pk"])
        self.assertFalse(by_name["name"]["nullable"])
        self.assertTrue(by_name["region"]["nullable"])

    def test_sqlite_execute(self):
        from app.services.db.sqlite import SQLiteConnector
        with SQLiteConnector(self.kb) as conn:
            rows = conn.execute("SELECT name, region FROM customers ORDER BY id", max_rows=10)
        self.assertEqual([r["name"] for r in rows], ["Alice", "Bob", "Carol"])
        self.assertEqual(rows[0]["region"], "EU")

    def test_sqlite_max_rows(self):
        from app.services.db.sqlite import SQLiteConnector
        with SQLiteConnector(self.kb) as conn:
            rows = conn.execute("SELECT * FROM orders", max_rows=2)
        self.assertEqual(len(rows), 2)

    def test_sqlite_test_connection(self):
        from app.services.db.sqlite import SQLiteConnector
        with SQLiteConnector(self.kb) as conn:
            result = conn.test_connection()
        self.assertTrue(result["ok"])
        self.assertIn("SQLite", result["info"])


class _SchemaAndQuery(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        con = sqlite3.connect(self.path)
        try:
            con.executescript(
                """
                CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, region TEXT);
                INSERT INTO customers (id, name, region) VALUES
                  (1, 'Alice', 'EU'), (2, 'Bob', 'EU'), (3, 'Carol', 'US');
                """
            )
            con.commit()
        finally:
            con.close()
        self.kb = _make_kb_stub(self.path)

        # In-memory engine + the real model metadata so SchemaService / QueryService
        # find the `knowledge_bases` table.
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.database import Base
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self._persist_kb()

    def tearDown(self) -> None:
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _persist_kb(self):
        from app.models.knowledge_base import KnowledgeBase
        row = KnowledgeBase(
            id=self.kb.id,
            name=self.kb.name,
            source_kind=self.kb.source_kind,
            db_type=self.kb.db_type,
            host=self.kb.host,
            port=self.kb.port,
            database_name=self.kb.database_name,
            username=self.kb.username,
            password=self.kb.password,
            api_url=self.kb.api_url,
            file_url=self.kb.file_url,
            status="active",
        )
        self.db.add(row)
        self.db.commit()

    def test_schema_describe_all(self):
        from app.services.db.schema_service import SchemaService
        svc = SchemaService(self.db)
        out = svc.describe_all(self.kb.id, max_tables=10)
        self.assertEqual(out["source"]["id"], self.kb.id)
        names = {t["table"] for t in out["tables"]}
        self.assertIn("customers", names)

    def test_schema_describe_table(self):
        from app.services.db.schema_service import SchemaService
        svc = SchemaService(self.db)
        out = svc.describe_table(self.kb.id, "customers")
        self.assertEqual(out["table"], "customers")
        cols = {c["name"] for c in out["columns"]}
        self.assertIn("id", cols)

    def test_query_execute(self):
        from app.services.db.query_service import QueryService
        svc = QueryService(self.db)
        res = svc.execute(
            self.kb.id,
            "SELECT region, COUNT(*) AS n FROM customers GROUP BY region ORDER BY region",
            max_rows=50,
            timeout_s=5,
        )
        by_region = {r["region"]: r["n"] for r in res["rows"]}
        self.assertEqual(by_region, {"EU": 2, "US": 1})
        self.assertEqual(res["row_count"], 2)
        self.assertFalse(res["truncated"])

    def test_query_rejects_empty(self):
        from app.services.db.query_service import QueryService
        svc = QueryService(self.db)
        with self.assertRaises(ValueError):
            svc.execute(self.kb.id, "   ")

    def test_query_rejects_non_database(self):
        from app.services.db.query_service import QueryService
        from app.models.knowledge_base import KnowledgeBase
        # Flip the row's source_kind to a non-database value directly.
        self.db.query(KnowledgeBase).filter(KnowledgeBase.id == self.kb.id).update(
            {"source_kind": "file"}
        )
        self.db.commit()
        svc = QueryService(self.db)
        with self.assertRaises(ValueError):
            svc.execute(self.kb.id, "SELECT 1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
