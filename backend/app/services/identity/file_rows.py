"""P3a — structured row staging for spreadsheet knowledge bases.

Uploaded xlsx/csv previously existed only as TEXT chunks (RAG). Identity
resolution needs queryable records, so tabular files are ALSO parsed into
``file_table_schemas`` / ``file_table_rows`` staging:

- header = first non-empty row; every later row becomes a values_json dict
- column type inference: int → float → string (sampled)
- bounded (max_rows, default 10 000) with an HONEST truncation flag
- idempotent per (kb, sheet): re-ingest replaces that sheet's rows
- file paths resolve via the upload-root-confined helper (traversal-proof)

Staging rows are COPIES for matching only — the uploaded file and the
source systems stay authoritative; nothing writes back.
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.identity import FileTableSchema, FileTableRow
from app.models.knowledge_base import KnowledgeBase

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROWS = 10_000

_TABULAR_TYPES = ("xlsx", "xls", "excel", "csv")


def is_tabular(kb: KnowledgeBase) -> bool:
    return (kb.file_type or "").lower().strip() in _TABULAR_TYPES


def _infer_type(values: list) -> str:
    """Sampled inference: int → float → string. None values ignored."""
    saw_float = saw_int = False
    for v in values:
        if v is None or isinstance(v, str) and not v.strip():
            continue
        if isinstance(v, bool):
            return "string"
        if isinstance(v, int):
            saw_int = True
        elif isinstance(v, float):
            saw_float = True
        elif isinstance(v, str):
            s = v.strip()
            try:
                int(s)
                saw_int = True
            except ValueError:
                try:
                    float(s)
                    saw_float = True
                except ValueError:
                    return "string"
    if saw_float:
        return "float"      # numeric superset (int+float mix → float)
    if saw_int:
        return "integer"
    return "string"


def _coerce(v):
    """All staged values are str for uniform cross-source comparison."""
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip() if isinstance(v, str) else str(v)


def ingest_structured_rows(db: Session, kb: KnowledgeBase,
                           max_rows: int = DEFAULT_MAX_ROWS) -> dict:
    """Parse a tabular file KB into staging. Never raises for content
    problems — returns an honest report (errors included)."""
    from app.services.document_ingestion.service import _resolve_local_path

    report: dict = {"kb_id": kb.id, "sheets": 0, "rows": 0, "truncated": False}
    path = _resolve_local_path(kb.file_url or "")
    if path is None or not Path(path).exists():
        report["error"] = "file not found or outside upload root"
        return report

    ft = (kb.file_type or "").lower().strip()
    try:
        if ft == "csv":
            sheets = _read_csv(path)
        else:
            sheets = _read_xlsx(path)
    except Exception as exc:  # noqa: BLE001 — honest failure
        logger.warning("file_rows ingest failed for kb=%s: %s", kb.id, exc)
        report["error"] = f"parse failed: {exc}"
        return report

    now = datetime.now(timezone.utc)
    for sheet_name, rows in sheets.items():
        if not rows:
            continue
        header = [str(h).strip() for h in rows[0]]
        data_rows = rows[1:]
        truncated = len(data_rows) > max_rows
        kept = data_rows[:max_rows]
        # Idempotent per (kb, sheet): replace, never duplicate.
        db.query(FileTableRow).filter(
            FileTableRow.kb_id == kb.id,
            FileTableRow.sheet_name == sheet_name,
        ).delete()
        db.query(FileTableSchema).filter(
            FileTableSchema.kb_id == kb.id,
            FileTableSchema.sheet_name == sheet_name,
        ).delete()
        columns = [{
            "name": h,
            "inferred_type": _infer_type([r[i] if i < len(r) else None
                                          for r in kept]),
        } for i, h in enumerate(header)]
        db.add(FileTableSchema(
            org_id=kb.org_id, kb_id=kb.id, sheet_name=sheet_name,
            columns_json=columns, row_count=len(kept), truncated=truncated,
            parsed_at=now,
        ))
        for idx, r in enumerate(kept):
            values = {}
            for i, h in enumerate(header):
                values[h] = _coerce(r[i]) if i < len(r) else None
            db.add(FileTableRow(
                org_id=kb.org_id, kb_id=kb.id, sheet_name=sheet_name,
                row_idx=idx, values_json=values,
            ))
        report["sheets"] += 1
        report["rows"] += len(kept)
        report["truncated"] = report["truncated"] or truncated
    db.commit()
    return report


def _read_xlsx(path) -> dict[str, list[list]]:
    from openpyxl import load_workbook  # lazy

    wb = load_workbook(path, read_only=True, data_only=True)
    sheets: dict[str, list[list]] = {}
    try:
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                if any(c is not None for c in row):
                    rows.append(list(row))
            sheets[ws.title] = rows
    finally:
        wb.close()
    return sheets


def _read_csv(path) -> dict[str, list[list]]:
    name = Path(path).stem or "data"
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        rows = [list(r) for r in csv.reader(f) if any(c.strip() for c in r)]
    return {name: rows}


def ingest_tabular_kb(db: Session, kb: KnowledgeBase) -> dict:
    """Hook used after RAG indexing of tabular file KBs."""
    if kb.source_kind != "file" or not is_tabular(kb):
        return {"skipped": True, "reason": "not a tabular file KB"}
    return ingest_structured_rows(db, kb)
