"""Router tests: /knowledge_bases/upload-query-file (called directly).

Uploading a CSV/XLSX must register a KnowledgeBase row with db_type='sqlite'
and file_url = the materialized sqlite path — which is exactly what makes it
appear in the Database Query node picker and run through SQLiteConnector.
"""

import sys
import os
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.database import Base  # noqa: E402
import app.models  # noqa: F401,E402  register all models
from app.models.knowledge_base import KnowledgeBase  # noqa: E402
from app.routers import knowledge_bases as kb_mod  # noqa: E402
from app.services import file_datasource as fd  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(_engine)
_TestSession = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture
def db():
    Base.metadata.drop_all(_engine)
    Base.metadata.create_all(_engine)
    s = _TestSession()
    try:
        yield s
    finally:
        s.close()


def _user(uid="user-1"):
    return SimpleNamespace(id=uid)


class _FakeUpload:
    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self._data = data

    async def read(self):
        return self._data


@pytest.mark.asyncio
async def test_upload_csv_registers_sqlite_datasource(db, tmp_path, monkeypatch):
    monkeypatch.setattr(fd.settings, "UPLOAD_DIR", str(tmp_path))
    upload = _FakeUpload("sales.csv", b"product,price\nLaptop,1200\nMouse,25\n")

    row = await kb_mod.upload_query_file(
        app_id="test-app",
        file=upload,
        name="Q2 Sales",
        description="uploaded",
        project="global",
        user=_user(),
        db=db,
    )

    assert row["name"] == "Q2 Sales"
    assert row["db_type"] == "sqlite"
    assert row["source_kind"] == "database"
    assert row["file_type"] == "csv"
    assert row["item_count"] == 2
    assert row["file_url"].endswith(".db")
    assert os.path.exists(row["file_url"])

    conn = sqlite3.connect(row["file_url"])
    assert conn.execute("SELECT count(*) FROM sales").fetchone()[0] == 2
    conn.close()

    # Persisted + appears in the db_query picker's filter (db_type truthy).
    persisted = db.query(KnowledgeBase).filter(KnowledgeBase.id == row["id"]).first()
    assert persisted is not None
    assert persisted.db_type == "sqlite"


@pytest.mark.asyncio
async def test_upload_unsupported_extension_rejected(db, tmp_path, monkeypatch):
    monkeypatch.setattr(fd.settings, "UPLOAD_DIR", str(tmp_path))
    upload = _FakeUpload("notes.pdf", b"%PDF-1.4 fake")

    with pytest.raises(HTTPException) as exc:
        await kb_mod.upload_query_file(
            app_id="test-app", file=upload, name="", user=_user(), db=db,
        )
    assert exc.value.status_code == 400
    assert "csv" in exc.value.detail


@pytest.mark.asyncio
async def test_upload_empty_file_rejected(db, tmp_path, monkeypatch):
    monkeypatch.setattr(fd.settings, "UPLOAD_DIR", str(tmp_path))
    upload = _FakeUpload("empty.csv", b"")

    with pytest.raises(HTTPException) as exc:
        await kb_mod.upload_query_file(
            app_id="test-app", file=upload, name="", user=_user(), db=db,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_upload_garbage_csv_rejected(db, tmp_path, monkeypatch):
    monkeypatch.setattr(fd.settings, "UPLOAD_DIR", str(tmp_path))
    upload = _FakeUpload("bad.csv", b"\x00\x01 not a csv at all \xff")

    with pytest.raises(HTTPException) as exc:
        await kb_mod.upload_query_file(
            app_id="test-app", file=upload, name="", user=_user(), db=db,
        )
    assert exc.value.status_code == 400
