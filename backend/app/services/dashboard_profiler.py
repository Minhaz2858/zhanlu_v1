"""DB-agnostic data profiler for dashboard generation.

HARD DB-AGNOSTIC RULE (non-negotiable):
  * ZERO hardcoded table/column identifiers. No demo/ERP table names
    (e.g. erp_v_sale_orderentry), no PLANDATE / FALLAMOUNT / FDATE /
    forgid, nothing environment-specific.
  * Only standard SQL: COUNT(*), COUNT(DISTINCT c), COUNT(c), MIN(c),
    MAX(c), LIMIT n. NO DATE_FORMAT, to_char, ::date casts,
    backticks-in-handwritten-SQL, or any vendor-specific functions.
  * Every identifier is validated and quoted via quote_ident() at
    runtime (dialect-aware).
  * Date detection uses classify_column_type() (union of MySQL/Postgres
    type names) plus looks_like_iso_date() on sample values — never
    vendor date functions.
"""

from __future__ import annotations

import re

from app.services.db.base import _safe_jsonify, quote_ident
from app.services.db.connector_factory import get_connector
from app.services.db.schema_service import SchemaService
from app.models.knowledge_base import KnowledgeBase


def classify_column_type(sql_type: str | None) -> str:
    if not sql_type:
        return "unknown"
    t = sql_type.lower().strip()
    if t in ("date", "datetime", "timestamp") or t.startswith(("date", "datetime", "timestamp")):
        return "date"
    if t.startswith(("int", "bigint", "smallint", "decimal", "numeric", "float", "double", "real", "money")):
        return "number"
    if t.startswith(("char", "varchar", "nchar", "nvarchar", "text", "clob", "string")):
        return "text"
    return "unknown"


_ISO_DATE_RE = re.compile(r"^\d{4}[/-]\d{1,2}[/-]\d{1,2}([T ]\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?)?$")


def looks_like_iso_date(value: object) -> bool:
    if value is None:
        return False
    s = str(value).strip()
    return bool(_ISO_DATE_RE.match(s))


def infer_shape(col_type: str, row_count: int, cardinality: int, null_pct: float, sample_count: int) -> str:
    if row_count <= 0 or cardinality <= 0 or null_pct >= 1.0:
        return "empty"
    if null_pct > 0.5:
        return "sparse"
    if col_type == "date":
        return "time_series"
    if col_type == "number":
        return "category" if cardinality <= 30 else "continuous"
    return "category"


def build_profile_queries(table: str, columns: list[str], sample_limit: int = 3) -> list[str]:
    """Build bounded, dialect-neutral profile SQL for a table and its columns.

    Only standard SQL is emitted (COUNT(*), COUNT(DISTINCT c), COUNT(c),
    MIN(c), MAX(c), LIMIT n). Every identifier goes through quote_ident()
    so it is validated and dialect-quoted at runtime.
    """
    tq = quote_ident(table, "default")
    queries = [f"SELECT COUNT(*) AS row_count FROM {tq}"]
    for col in columns:
        cq = quote_ident(col, "default")
        queries.append(
            f"SELECT COUNT(DISTINCT {cq}) AS cardinality, "
            f"COUNT({cq}) AS non_null, "
            f"MIN({cq}) AS min_value, MAX({cq}) AS max_value FROM {tq}"
        )
        queries.append(f"SELECT {cq} AS sample_value FROM {tq} LIMIT {sample_limit}")
    return queries


def profile_engine(
    db_uri_or_path: str,
    table: str,
    columns: list[str],
    sample_limit: int = 3,
    max_columns: int = 20,
) -> dict:
    """Profile a table in a SQLite file/URI and return a JSON-ready dict.

    Runs only standard SQL (COUNT(*), COUNT(DISTINCT), COUNT, MIN, MAX,
    LIMIT) with every identifier quoted via quote_ident(..., "sqlite").
    Never raises: failures are reported as status="error" with a
    truncated error_message (200 chars).
    """
    import sqlite3

    result: dict = {
        "table": table,
        "row_count": 0,
        "status": "ok",
        "error_message": None,
        "columns": [],
    }
    try:
        con = sqlite3.connect(db_uri_or_path)
        try:
            cur = con.cursor()
            tq = quote_ident(table, "sqlite")
            cur.execute(f"SELECT COUNT(*) AS row_count FROM {tq}")
            row = cur.fetchone()
            result["row_count"] = int(row[0]) if row and row[0] is not None else 0

            # SQLite silently treats unknown double-quoted identifiers as
            # string literals inside aggregates, so validate requested
            # columns against the real schema up front for deterministic
            # per-column errors.
            cur.execute(f"PRAGMA table_info({tq})")
            real_cols = {str(r[1]).casefold() for r in cur.fetchall()}

            for col in columns[:max_columns]:
                try:
                    if col.casefold() not in real_cols:
                        raise ValueError(f"Unknown column: {col!r}")
                    cq = quote_ident(col, "sqlite")
                    cur.execute(
                        f"SELECT COUNT(DISTINCT {cq}) AS cardinality, "
                        f"COUNT({cq}) AS non_null, "
                        f"MIN({cq}) AS min_value, MAX({cq}) AS max_value FROM {tq}"
                    )
                    stats = cur.fetchone()
                    cardinality = int(stats[0]) if stats and stats[0] is not None else 0
                    non_null = int(stats[1]) if stats and stats[1] is not None else 0
                    min_value = _safe_jsonify(stats[2]) if stats else None
                    max_value = _safe_jsonify(stats[3]) if stats else None

                    cur.execute(f"SELECT {cq} AS sample_value FROM {tq} LIMIT {sample_limit}")
                    samples = [r[0] for r in cur.fetchall()]
                    top_values = [_safe_jsonify(v) for v in samples if v is not None][:sample_limit]

                    null_pct = (
                        1.0 - (non_null / result["row_count"])
                        if result["row_count"] > 0 else 1.0
                    )
                    col_type = "text"
                    if any(looks_like_iso_date(v) for v in samples):
                        col_type = "date"
                    shape = infer_shape(
                        col_type, result["row_count"], cardinality,
                        null_pct, len(samples),
                    )
                    result["columns"].append({
                        "name": col,
                        "type": col_type,
                        "cardinality": cardinality,
                        "null_pct": null_pct,
                        "min": min_value,
                        "max": max_value,
                        "top_values": top_values,
                        "shape": shape,
                    })
                except Exception as e:  # per-column failure
                    result["status"] = "error"
                    result["error_message"] = str(e)[:200]
                    break

            if result["status"] == "ok" and result["row_count"] == 0:
                result["status"] = "empty"
        finally:
            con.close()
    except Exception as e:  # table-level failure
        result["status"] = "error"
        result["error_message"] = str(e)[:200]
    return result


def _load_kb(db, kb_id: str) -> KnowledgeBase:
    """Load a non-deleted KnowledgeBase row, raising ValueError if missing."""
    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )
    if not kb:
        raise ValueError(f"KnowledgeBase not found: {kb_id}")
    return kb


def _infer_col_type(db, kb_id: str, table: str, col: str, samples: list) -> str:
    """Best-effort column type classification for the connector path.

    Tries the live schema (SchemaService.describe_table) first via
    classify_column_type(), then falls back to looking at sample values
    (looks_like_iso_date -> "date", else "text"). Never raises.
    """
    try:
        info = SchemaService(db).describe_table(kb_id, table)
        for c in info.get("columns", []):
            if str(c.get("name", "")).lower() == col.lower():
                sql_type = c.get("type") or c.get("data_type")
                if sql_type:
                    return classify_column_type(sql_type)
                break
    except Exception:
        pass
    if any(looks_like_iso_date(v) for v in samples):
        return "date"
    return "text"


def profile_kb(
    db,
    kb_id: str,
    table: str,
    columns: list[str],
    sample_limit: int = 3,
    max_columns: int = 20,
    timeout_s: int = 12,
) -> dict:
    """Profile a table on a live KnowledgeBase connection (dialect-aware).

    Dialect comes from kb.db_type; every identifier is validated and
    quoted via quote_ident(name, dialect). Only standard SQL is emitted.
    Never raises: failures are reported as status="error" with a truncated
    error_message (200 chars).
    """
    result: dict = {
        "table": table,
        "row_count": 0,
        "status": "ok",
        "error_message": None,
        "columns": [],
    }
    try:
        kb = _load_kb(db, kb_id)
        dialect = kb.db_type or "default"
        tq = quote_ident(table, dialect)
        with get_connector(kb) as conn:
            rows = conn.execute(
                f"SELECT COUNT(*) AS row_count FROM {tq}",
                max_rows=5, timeout_s=timeout_s,
            )
            result["row_count"] = int(rows[0]["row_count"]) if rows else 0

            # Validate requested columns against the live schema up front.
            # Some engines (SQLite especially) silently coerce unknown
            # double-quoted identifiers into string literals inside
            # aggregates, which would yield bogus "profiles" — fail
            # deterministically instead of returning garbage.
            try:
                schema_info = SchemaService(db).describe_table(kb_id, table)
                real_cols = {
                    str(c.get("name") or c.get("column_name") or "").casefold()
                    for c in schema_info.get("columns", [])
                }
            except Exception as e:
                result["status"] = "error"
                result["error_message"] = str(e)[:200]
                return result

            for col in columns[:max_columns]:
                try:
                    if str(col).casefold() not in real_cols:
                        raise ValueError(f"unknown column: {col}")
                    cq = quote_ident(col, dialect)
                    stats = conn.execute(
                        f"SELECT COUNT(DISTINCT {cq}) AS cardinality, "
                        f"COUNT({cq}) AS non_null, "
                        f"MIN({cq}) AS min_value, MAX({cq}) AS max_value FROM {tq}",
                        max_rows=5, timeout_s=timeout_s,
                    )
                    s = stats[0] if stats else {}
                    cardinality = int(s.get("cardinality") or 0)
                    non_null = int(s.get("non_null") or 0)
                    min_value = _safe_jsonify(s.get("min_value"))
                    max_value = _safe_jsonify(s.get("max_value"))

                    sample_rows = conn.execute(
                        f"SELECT {cq} AS sample_value FROM {tq} LIMIT {sample_limit}",
                        max_rows=sample_limit, timeout_s=timeout_s,
                    )
                    sample_values = [r.get("sample_value") for r in sample_rows]
                    top_values = [_safe_jsonify(v) for v in sample_values if v is not None][:sample_limit]

                    null_pct = (
                        1.0 - (non_null / result["row_count"])
                        if result["row_count"] > 0 else 1.0
                    )
                    col_type = _infer_col_type(db, kb_id, table, col, sample_values)
                    shape = infer_shape(
                        col_type, result["row_count"], cardinality,
                        null_pct, len(sample_values),
                    )
                    result["columns"].append({
                        "name": col,
                        "type": col_type,
                        "cardinality": cardinality,
                        "null_pct": null_pct,
                        "min": min_value,
                        "max": max_value,
                        "top_values": top_values,
                        "shape": shape,
                    })
                except Exception as e:  # per-column failure
                    result["status"] = "error"
                    result["error_message"] = str(e)[:200]
                    break

            if result["status"] == "ok" and result["row_count"] == 0:
                result["status"] = "empty"
    except Exception as e:  # table-level / connector failure
        result["status"] = "error"
        result["error_message"] = str(e)[:200]
    return result
