"""P3a — structured row staging for spreadsheets (xlsx/csv).

Files uploaded as knowledge bases previously became TEXT chunks only;
identity resolution needs queryable records. These tests pin:
- deterministic sheet → schema + rows ingestion (xlsx and csv)
- column type inference (int → float → string)
- bounded ingestion with honest truncation
- idempotent re-ingest per (kb, sheet) — replace, never duplicate
- traversal-safe file resolution (upload root confinement)
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.identity import (  # noqa: F401 — register before create_all
    FileTableSchema,
    FileTableRow,
    IdentityMatch,
)
from app.models.knowledge_base import KnowledgeBase

ORG = "org-test"


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
def kb(db):
    k = KnowledgeBase(
        org_id=ORG, name="customer master", source_kind="file",
        file_type="xlsx",
    )
    db.add(k)
    db.commit()
    return k


def _write_xlsx(path: Path, sheet_rows: dict[str, list[list]]):
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for sheet, rows in sheet_rows.items():
        ws = wb.create_sheet(title=sheet)
        for row in rows:
            ws.append(row)
    wb.save(path)


@pytest.fixture()
def upload_root(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    root.mkdir()
    import app.services.document_ingestion.service as svc

    monkeypatch.setattr(svc, "_upload_root", lambda: root)
    yield root


# ── XLSX ────────────────────────────────────────────────────────────────


def test_ingest_xlsx_builds_schema_and_rows(db, kb, upload_root, monkeypatch):
    from app.models.identity import FileTableSchema, FileTableRow
    from app.services.identity.file_rows import ingest_structured_rows

    path = upload_root / "customer_master.xlsx"
    _write_xlsx(path, {"Customers": [
        ["cust_id", "cust_name", "credit"],
        [1001, "Acme GmbH", 1500.5],
        ["C1002", "Bravo Ltd", 200],
        [1003, "Charlie SARL", None],
    ]})
    monkeypatch.setattr(kb, "file_url", "/api/uploads/customer_master.xlsx")

    report = ingest_structured_rows(db, kb)
    db.commit()

    assert report["sheets"] == 1 and report["rows"] == 3
    assert report["truncated"] is False
    schema = db.query(FileTableSchema).filter_by(
        kb_id=kb.id, sheet_name="Customers").one()
    cols = {c["name"]: c["inferred_type"] for c in schema.columns_json}
    assert cols == {"cust_id": "string", "cust_name": "string",
                    "credit": "float"}  # mixed int/str ids → string
    assert schema.row_count == 3
    rows = (db.query(FileTableRow)
            .filter_by(kb_id=kb.id, sheet_name="Customers")
            .order_by(FileTableRow.row_idx).all())
    assert rows[0].values_json["cust_id"] == "1001"
    assert rows[1].values_json["cust_id"] == "C1002"
    assert rows[2].values_json["credit"] is None


def test_ingest_replaces_sheet_on_reingest(db, kb, upload_root, monkeypatch):
    """Idempotent per (kb, sheet): re-ingest replaces rows, never duplicates."""
    from app.models.identity import FileTableRow
    from app.services.identity.file_rows import ingest_structured_rows

    path = upload_root / "cm.xlsx"
    _write_xlsx(path, {"S": [["id"], [1], [2]]})
    monkeypatch.setattr(kb, "file_url", "/api/uploads/cm.xlsx")
    ingest_structured_rows(db, kb)
    db.commit()
    _write_xlsx(path, {"S": [["id"], [1], [2], [3]]})
    ingest_structured_rows(db, kb)
    db.commit()
    rows = db.query(FileTableRow).filter_by(kb_id=kb.id, sheet_name="S").all()
    assert len(rows) == 3


def test_ingest_bounds_rows_honestly(db, kb, upload_root, monkeypatch):
    from app.models.identity import FileTableSchema, FileTableRow
    from app.services.identity.file_rows import ingest_structured_rows

    path = upload_root / "big.xlsx"
    _write_xlsx(path, {"S": [["id"]] + [[i] for i in range(50)]})
    monkeypatch.setattr(kb, "file_url", "/api/uploads/big.xlsx")
    report = ingest_structured_rows(db, kb, max_rows=10)
    db.commit()
    assert report["rows"] == 10 and report["truncated"] is True
    schema = db.query(FileTableSchema).filter_by(
        kb_id=kb.id, sheet_name="S").one()
    assert schema.truncated is True
    assert db.query(FileTableRow).filter_by(kb_id=kb.id).count() == 10


def test_ingest_rejects_paths_outside_upload_root(db, kb, monkeypatch):
    from app.services.identity.file_rows import ingest_structured_rows

    monkeypatch.setattr(kb, "file_url", "/api/uploads/../../etc/passwd")
    report = ingest_structured_rows(db, kb)
    assert report["sheets"] == 0 and "error" in report


# ── CSV ─────────────────────────────────────────────────────────────────


def test_ingest_csv(db, kb, upload_root, monkeypatch):
    from app.services.identity.file_rows import ingest_structured_rows

    path = upload_root / "suppliers.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["supp_id", "supp_name"])
        w.writerow(["C1001", "Delta Co"])
        w.writerow(["1002", "Echo AG"])
    monkeypatch.setattr(kb, "file_url", "/api/uploads/suppliers.csv")
    monkeypatch.setattr(kb, "file_type", "csv")

    report = ingest_structured_rows(db, kb)
    db.commit()
    assert report["sheets"] == 1 and report["rows"] == 2
    schema = db.query(FileTableSchema).filter_by(kb_id=kb.id).one()
    assert schema.columns_json[0]["name"] == "supp_id"


# ── Value accessor used by the resolver ─────────────────────────────────


def test_fetch_source_values_from_staged_rows(db, kb, upload_root, monkeypatch):
    from app.services.identity.resolver import fetch_source_values

    path = upload_root / "cm.xlsx"
    _write_xlsx(path, {"Customers": [
        ["cust_id", "cust_name"],
        [1001, "Acme GmbH"],
        ["C1002", "Bravo Ltd"],
    ]})
    monkeypatch.setattr(kb, "file_url", "/api/uploads/cm.xlsx")
    from app.services.identity.file_rows import ingest_structured_rows

    ingest_structured_rows(db, kb)
    db.commit()

    source = {"kind": "file", "kb_id": kb.id, "sheet": "Customers",
              "column": "cust_id", "pk_column": "cust_id"}
    values = fetch_source_values(db, ORG, source, limit=100)
    assert sorted(values, key=str) == ["1001", "C1002"]


def test_fetch_source_values_rejects_cross_org(db, kb, monkeypatch):
    from app.services.identity.resolver import fetch_source_values

    monkeypatch.setattr(kb, "file_url", "/api/uploads/cm.xlsx")
    source = {"kind": "file", "kb_id": kb.id, "sheet": "S",
              "column": "id", "pk_column": "id"}
    assert fetch_source_values(db, "other-org", source) == []
