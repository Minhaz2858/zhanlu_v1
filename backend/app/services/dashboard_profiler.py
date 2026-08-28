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
from datetime import datetime  # noqa: F401  (kept for API parity/future use)

from app.services.db.base import quote_ident


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
