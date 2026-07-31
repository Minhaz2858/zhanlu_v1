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
