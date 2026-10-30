"""P3a wiring — tabular file KBs get their rows staged as part of (and
in addition to) normal RAG indexing; explicit re-stage endpoint."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.identity import FileTableRow
from app.models.knowledge_base import KnowledgeBase

ORG = "org-hook"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()
    eng.dispose()


@pytest.fixture()
def xlsx_kb(db, monkeypatch):
    import app.services.document_ingestion.service as svc
    from openpyxl import Workbook

    root = Path(tempfile.mkdtemp())
    svc._upload_root = lambda: root

    p = root / "crm.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "CRM"
    ws.append(["account_id", "account_name"])
    ws.append([1001, "Acme"])
    ws.append([1002, "Bravo"])
    wb.save(p)

    kb = KnowledgeBase(org_id=ORG, name="crm", source_kind="file",
                       file_type="xlsx", file_url="/api/uploads/crm.xlsx")
    db.add(kb)
    db.commit()

    # RAG side effects stubbed — we are testing the staging hook only.
    monkeypatch.setattr(svc.store, "delete_kb", lambda **kw: None)
    monkeypatch.setattr(svc.store, "upsert_chunks",
                        lambda **kw: 1, raising=True)
    return kb


def test_ingest_kb_also_stages_rows(db, xlsx_kb):
    from app.services.document_ingestion import service as svc

    ok = svc.ingest_kb(db, xlsx_kb.id)
    assert ok is True
    rows = db.query(FileTableRow).filter_by(kb_id=xlsx_kb.id).all()
    assert len(rows) == 2
    assert {r.values_json["account_id"] for r in rows} == {"1001", "1002"}


def test_ingest_rows_endpoint_restages(db, xlsx_kb):
    import asyncio

    from app.routers.knowledge_bases import ingest_rows_endpoint

    db.query(FileTableRow).filter_by(kb_id=xlsx_kb.id).delete()
    db.commit()

    report = asyncio.run(ingest_rows_endpoint(kb_id=xlsx_kb.id, db=db))
    assert report["rows"] == 2
    assert db.query(FileTableRow).filter_by(kb_id=xlsx_kb.id).count() == 2


def test_ingest_rows_rejects_non_tabular(db, xlsx_kb):
    import asyncio

    from fastapi import HTTPException

    from app.routers.knowledge_bases import ingest_rows_endpoint

    xlsx_kb.file_type = "pdf"
    db.commit()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(ingest_rows_endpoint(kb_id=xlsx_kb.id, db=db))
    assert ei.value.status_code == 400


def test_ingest_rows_unknown_kb_404(db):
    import asyncio

    from fastapi import HTTPException

    from app.routers.knowledge_bases import ingest_rows_endpoint

    with pytest.raises(HTTPException) as ei:
        asyncio.run(ingest_rows_endpoint(kb_id="nope", db=db))
    assert ei.value.status_code == 404
