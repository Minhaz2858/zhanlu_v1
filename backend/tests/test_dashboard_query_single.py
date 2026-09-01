import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import sqlite3
import tempfile
import os as _os

import pytest

from app.database import Base, engine, SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.services.dashboard_query import _run_single_sql
import app.models  # noqa: F401  register all models


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def sqlite_kb(db):
    """A connectable sqlite KB (api_url = temp .db file with one row)."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.execute("CREATE TABLE metrics (n INTEGER, label TEXT)")
    conn.execute("INSERT INTO metrics VALUES (1, 'a')")
    conn.commit()
    conn.close()
    kb = KnowledgeBase(name="t", source_kind="database", db_type="sqlite",
                       api_url=f.name, org_id="default-org", app_id="default-app")
    db.add(kb)
    db.commit()
    yield kb
    _os.unlink(f.name)


def test_run_single_sql_returns_rows(sqlite_kb, db):
    import asyncio
    r = asyncio.run(_run_single_sql(db, sqlite_kb.id, "SELECT n, label FROM metrics", None, [], 1000, 10))
    assert r["error"] is None
    assert r["columns"] == ["n", "label"]
    assert r["rows"] == [{"n": 1, "label": "a"}]
    assert r["truncated"] is False


def test_run_single_sql_rejects_non_readonly(sqlite_kb, db):
    import asyncio
    r = asyncio.run(_run_single_sql(db, sqlite_kb.id, "DELETE FROM metrics", None, [], 1000, 10))
    assert r["error"]  # validate_widget_sql raises inside render → caught
    assert r["rows"] == []


def test_run_single_sql_unknown_token(sqlite_kb, db):
    import asyncio
    r = asyncio.run(_run_single_sql(db, sqlite_kb.id, "SELECT :foo", None, [], 1000, 10))
    assert r["error"]  # render_widget_sql rejects unknown :foo token
