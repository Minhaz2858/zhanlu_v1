"""Tests for the M-Schema renderer — produces a <m-schema> text block for LLM context."""

import sqlite3
import pytest
from app.services.datasources.sqlite_adapter import SQLiteAdapter


def test_render_m_schema_includes_table_comment_and_examples(tmp_path):
    """The M-Schema block must include table name, column names/types, and example values."""
    db = tmp_path / "t.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, amount REAL)")
    con.execute("INSERT INTO orders VALUES (1,'EU',100),(2,'US',50),(3,'EU',75)")
    con.commit()
    con.close()
    adapter = SQLiteAdapter(db_path=str(db))
    from app.services.datasources.m_schema import render_m_schema
    out = render_m_schema(adapter, allowed_tables=["orders"], sample_rows=2)
    assert "# Table: orders" in out
    assert "region" in out
    assert "amount" in out
    assert "EU" in out  # example value from low-cardinality column


def test_render_m_schema_respects_allowed_tables(tmp_path):
    """Only requested tables appear in the output."""
    db = tmp_path / "t2.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE t1 (a TEXT)")
    con.execute("CREATE TABLE t2 (b TEXT)")
    con.commit()
    con.close()
    adapter = SQLiteAdapter(db_path=str(db))
    from app.services.datasources.m_schema import render_m_schema
    out = render_m_schema(adapter, allowed_tables=["t1"], sample_rows=0)
    assert "# Table: t1" in out
    assert "t2" not in out or "# Table: t2" not in out


def test_render_m_schema_marks_pk(tmp_path):
    """Primary key columns get a [PK] tag."""
    db = tmp_path / "t3.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE x (id INTEGER PRIMARY KEY, name TEXT)")
    con.commit()
    con.close()
    adapter = SQLiteAdapter(db_path=str(db))
    from app.services.datasources.m_schema import render_m_schema
    out = render_m_schema(adapter, allowed_tables=["x"], sample_rows=0)
    assert "id:" in out
    assert "[PK]" in out


def test_render_m_schema_handles_empty_schema(tmp_path):
    """An empty database produces empty output, not a crash."""
    db = tmp_path / "t4.db"
    con = sqlite3.connect(str(db))
    con.commit()
    con.close()
    adapter = SQLiteAdapter(db_path=str(db))
    from app.services.datasources.m_schema import render_m_schema
    out = render_m_schema(adapter, sample_rows=0)
    assert isinstance(out, str)


def test_is_blob_column_detects_unbounded_blobs():
    """Unbounded blob raw types are skipped; bounded/varchar types are not."""
    from app.services.datasources.m_schema import _is_blob_column
    from app.services.datasources import ColumnInfo

    # MySQL normalises these to TEXT but keeps raw_type in extra
    assert _is_blob_column(ColumnInfo("body", "TEXT", extra={"raw_type": "longtext"}))
    assert _is_blob_column(ColumnInfo("cfg", "TEXT", extra={"raw_type": "json"}))
    assert _is_blob_column(ColumnInfo("note", "TEXT", extra={"raw_type": "mediumtext"}))

    # Bounded / native TEXT and varchar are sampleable
    assert not _is_blob_column(ColumnInfo("region", "TEXT"))
    assert not _is_blob_column(ColumnInfo("name", "VARCHAR", extra={"raw_type": "varchar(255)"}))
    assert not _is_blob_column(ColumnInfo("status", "TEXT", extra={"raw_type": "enum('a','b')"}))


def test_render_m_schema_skips_blob_columns(tmp_path, monkeypatch):
    """Columns whose raw type is an unbounded blob are never DISTINCT-sampled."""
    from app.services.datasources import ColumnInfo
    from app.services.datasources.m_schema import render_m_schema

    class FakeAdapter:
        quote_char = '"'

        def refresh_schema(self):
            return {
                "t": [
                    ColumnInfo("region", "TEXT"),
                    ColumnInfo("body", "TEXT", extra={"raw_type": "longtext"}),
                ]
            }

        def query(self, sql, row_limit=1000, timeout_ms=5000):
            # Record sampled columns; never called for the blob.
            sampled.add(sql)
            from app.services.datasources import QueryResult
            return QueryResult(columns=["x"], rows=[("EU",)], row_count=1, duration_ms=1)

    sampled = set()
    out = render_m_schema(FakeAdapter(), allowed_tables=["t"], sample_rows=2)
    joined = " ".join(sampled)
    assert '"region"' in joined
    assert "body" not in joined


def test_render_m_schema_respects_sample_budget(tmp_path):
    """Sampling stops early once the wall-clock budget is exhausted."""
    from app.services.datasources import ColumnInfo
    from app.services.datasources.m_schema import render_m_schema

    class SlowAdapter:
        quote_char = '"'

        def refresh_schema(self):
            return {"t": [ColumnInfo(f"c{i}", "TEXT") for i in range(100)]}

        def query(self, sql, row_limit=1000, timeout_ms=5000):
            import time
            time.sleep(0.2)
            from app.services.datasources import QueryResult
            return QueryResult(columns=["x"], rows=[("v",)], row_count=1, duration_ms=200)

    out = render_m_schema(SlowAdapter(), allowed_tables=["t"], sample_rows=1, sample_budget_ms=300)
    # Only a couple columns sampled before the 300ms budget hit — not all 100.
    sampled = out.count("examples:")
    assert 0 < sampled < 100
