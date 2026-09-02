"""Spreadsheet → queryable SQLite datasource.

Uploaded CSV/XLSX files become local SQLite databases that the workflow
Database Query node can query like any connected database (SQL + natural
language). The KnowledgeBase row for such a file carries ``db_type='sqlite'``
and ``file_url`` = the sqlite file path — the existing SQLiteConnector and
the governed nl2sql adapter already resolve sqlite connectors that way, so
no runtime changes are needed.

Tables/columns are sanitized (headers become clean identifiers) so both SQL
mode and the NL translator see predictable names.
"""

import io
import logging
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import settings

logger = logging.getLogger(__name__)

ALLOWED_EXTS = {".csv", ".xlsx", ".xls"}
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB

_IDENT_RE = re.compile(r"[^0-9a-zA-Z_]+")


def _clean_identifier(raw: str, used: set[str], fallback: str = "col") -> str:
    """Sanitize a header/table name into a safe lowercase SQL identifier, deduped."""
    s = _IDENT_RE.sub("_", str(raw).strip().replace(" ", "_")).strip("_").lower()
    s = re.sub(r"_+", "_", s)
    if not s or s[0].isdigit():
        s = f"{fallback}_{s}" if s else fallback
    base, i = s, 2
    while s in used:
        s = f"{base}_{i}"
        i += 1
    used.add(s)
    return s


def _write_df(db_path: Path, df: pd.DataFrame, table: str) -> int:
    """Write a DataFrame as a table; returns row count."""
    import sqlalchemy

    engine = sqlalchemy.create_engine(f"sqlite:///{db_path}")
    try:
        df.to_sql(name=table, con=engine, if_exists="replace", index=False)
    finally:
        engine.dispose()
    return int(len(df))


def import_spreadsheet_to_sqlite(file_bytes: bytes, filename: str) -> dict[str, Any]:
    """Parse an uploaded spreadsheet into a queryable SQLite file.

    Returns ``{"db_path": Path, "tables": [str], "total_rows": int}``.
    Raises ValueError for unsupported extensions / empty inputs.
    """
    if len(file_bytes) > MAX_FILE_BYTES:
        raise ValueError(f"File too large (max {MAX_FILE_BYTES // (1024 * 1024)} MB)")
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise ValueError(f"Unsupported file type '{ext}' — use .csv, .xlsx or .xls")

    db_dir = settings.upload_path / "file_datasources"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / f"{uuid.uuid4().hex}.db"
    if db_path.exists():
        db_path.unlink()

    stem = Path(filename).stem or "data"
    base_table = _clean_identifier(stem, set(), fallback="data")

    if ext == ".csv":
        try:
            df = pd.read_csv(io.BytesIO(file_bytes))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Could not parse CSV: {exc}") from exc
        if df.empty or df.columns.size == 0:
            raise ValueError("CSV has no usable data")
        used = set()
        df.columns = [_clean_identifier(c, used) for c in df.columns]
        rows = _write_df(db_path, df, base_table)
        return {"db_path": db_path, "tables": [base_table], "total_rows": rows}

    # .xlsx / .xls — one table per sheet
    try:
        sheets = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Could not parse spreadsheet: {exc}") from exc
    if not sheets:
        raise ValueError("Spreadsheet contains no sheets")

    tables: list[str] = []
    used_tables: set[str] = set()
    total = 0
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=OFF")
    for sheet_name, df in sheets.items():
        table = _clean_identifier(sheet_name, used_tables, fallback="sheet")
        if df is None or df.empty or df.columns.size == 0:
            continue
        used = set()
        df.columns = [_clean_identifier(c, used) for c in df.columns]
        rows = _write_df(db_path, df, table)
        tables.append(table)
        total += rows
    if not tables:
        raise ValueError("Spreadsheet has no sheets with usable data")
    return {"db_path": db_path, "tables": tables, "total_rows": total}
