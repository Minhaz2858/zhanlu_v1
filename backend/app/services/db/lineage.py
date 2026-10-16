"""Data lineage for NL2SQL — which tables and columns an executed query
actually read.

Closes the "lineage metadata MISSING" audit gap: every generated SQL
statement is parsed (sqlglot, dialect-aware; regex fallback) into a compact
lineage record — tables touched, columns referenced, join pairs, filtered
columns — and that record is (a) attached to the answer result so the
narration/UI can say where each figure came from, and (b) persisted on the
``nl2sql_query_logs.lineage_json`` telemetry row.

Design rules:
- Lineage is EXTRACTED from the statement that actually ran, never from the
  plan or the prompt — a discarded SQL candidate must not become provenance.
- Extraction failure degrades to a regex table scan; total failure returns
  None ("not computed") — never an empty dict pretending "no lineage".
- Lists are capped so a 40-table JOIN cannot blow up the log row.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

MAX_TABLES = 40
MAX_COLUMNS = 80
MAX_JOINS = 20
MAX_FILTERS = 20

_TABLE_REF_RE = re.compile(
    r"\b(?:from|join)\s+([\"'`\[]?)([A-Za-z_][\w.$]*)\1", re.IGNORECASE
)
_FLAG_RE = re.compile(r"^(true|false|null)$", re.I)


def _dialect_for_kb(kb: Any) -> str | None:
    """sqlglot dialect name for a KB, or None (best-effort)."""
    try:
        from app.services.db.execution_gate import _dialect_for

        return _dialect_for(kb)
    except Exception:
        return None


def _parse(sql: str, dialect: str | None):
    import sqlglot

    return sqlglot.parse_one(sql, dialect=dialect) if dialect else sqlglot.parse_one(sql)


def _dedupe_cap(values: list[str], cap: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
            if len(out) >= cap:
                break
    return out


def _extract_tables_columns(sql: str, dialect: str | None) -> dict:
    """sqlglot walk producing the raw lineage lists."""
    import sqlglot.expressions as e

    tree = _parse(sql, dialect)
    tables: list[str] = []
    columns: list[str] = []  # "table.column" or "column" when unqualified
    join_tables: list[str] = []
    filter_columns: list[str] = []

    for node in tree.find_all(e.Table):
        name = node.name
        if name:
            tables.append(name)

    for join in tree.find_all(e.Join):
        t = join.find(e.Table)
        if t is not None and t.name:
            join_tables.append(t.name)

    for col in tree.find_all(e.Column):
        ref = f"{col.table}.{col.name}" if col.table else col.name
        columns.append(ref)

    where = tree.find(e.Where)
    if where is not None:
        for col in where.find_all(e.Column):
            ref = f"{col.table}.{col.name}" if col.table else col.name
            filter_columns.append(ref)

    return {
        "tables": _dedupe_cap(tables, MAX_TABLES),
        "columns": _dedupe_cap(columns, MAX_COLUMNS),
        "join_tables": _dedupe_cap(join_tables, MAX_JOINS),
        "filter_columns": _dedupe_cap(filter_columns, MAX_FILTERS),
    }


def _extract_via_regex(sql: str) -> dict | None:
    """Degrade gracefully: table names only, no sqlglot."""
    if not sql or not sql.strip():
        return None
    tables: list[str] = []
    for _q, ident in _TABLE_REF_RE.findall(sql):
        name = (ident or "").split(".")[-1].strip("`\"[]")
        if name and not _FLAG_RE.match(name):
            tables.append(name)
    if not tables:
        return None
    return {
        "tables": _dedupe_cap(tables, MAX_TABLES),
        "columns": [],
        "join_tables": [],
        "filter_columns": [],
        "extraction": "regex_fallback",
    }


def extract_lineage(sql: str | None, kb: Any = None) -> dict | None:
    """Public extractor: sqlglot walk with regex fallback. None = not computed."""
    if not sql or not sql.strip():
        return None
    dialect = _dialect_for_kb(kb)
    try:
        lineage = _extract_tables_columns(sql, dialect)
    except Exception as e:  # noqa: BLE001
        logger.debug("lineage: sqlglot extraction failed (%s) — regex fallback", e)
        lineage = _extract_via_regex(sql)
    if lineage is None:
        return None
    if dialect:
        lineage["dialect"] = dialect
    return lineage


def format_lineage_block(lineage: dict | None) -> str:
    """Human-readable provenance sentence(s) for an answer/UI disclosure."""
    if not lineage:
        return ""
    parts: list[str] = []
    tables = lineage.get("tables") or []
    if tables:
        parts.append("tables: " + ", ".join(tables[:8]) + ("…" if len(tables) > 8 else ""))
    cols = lineage.get("columns") or []
    if cols:
        parts.append("columns: " + ", ".join(cols[:12]) + ("…" if len(cols) > 12 else ""))
    if not parts:
        return ""
    return "Data lineage (" + "; ".join(parts) + ")"
