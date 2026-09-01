"""M-Schema renderer — produce a <m-schema> text block the LLM can read.

Used by the NL2SQL prompt. Format mirrors the BIRD / Spider M-Schema convention
so the LLM sees table comment, columns (name, type, comment, PK, examples).
"""

from __future__ import annotations

import logging
import re
import time

from app.services.datasources import DatasourceAdapter

logger = logging.getLogger(__name__)

# Value sampling runs one ``SELECT DISTINCT <col> FROM <table>`` per TEXT-like
# column. On a warehouse with hundreds of tables/columns this is prohibitively
# slow (measured ~28 minutes on a 139-table MySQL KB) and produces huge
# prompts. Two guards keep it fast: (1) unbounded blob columns are skipped,
# and (2) a total wall-clock budget truncates the rest.
_SAMPLEABLE_TYPES = frozenset({
    "TEXT",
    "VARCHAR",
    "CHAR",
    "CHARACTER VARYING",
    "ENUM",
    "SET",
})

# Raw (driver-level) base types that are unbounded blobs — free-form text or
# serialized data that is both expensive to DISTINCT over and useless as a
# schema example. MySQL normalises ``longtext``/``text``/``blob``/``json`` to
# ``TEXT`` in ``dtype``, so we consult ``extra["raw_type"]`` to tell them apart
# from the bounded ``TEXT`` that SQLite reports natively.
_BLOB_RAW_TYPES = frozenset({
    "text",
    "longtext",
    "mediumtext",
    "tinytext",
    "json",
    "jsonb",
    "blob",
    "tinyblob",
    "mediumblob",
    "longblob",
})

# Default cap on total wall-clock seconds spent sampling values. Sampling is
# best-effort (distinct value examples), so we truncate rather than block.
DEFAULT_SAMPLE_BUDGET_MS = 8000


def _is_blob_column(col: object) -> bool:
    """True when a column's raw type is an unbounded blob (skip sampling)."""
    raw = str((getattr(col, "extra", None) or {}).get("raw_type", "") or "")
    base = re.sub(r"\(.*\)", "", raw).strip().lower()
    return base in _BLOB_RAW_TYPES


def render_m_schema(
    adapter: DatasourceAdapter,
    allowed_tables: list[str] | None = None,
    sample_rows: int = 3,
    sample_budget_ms: int = DEFAULT_SAMPLE_BUDGET_MS,
) -> str:
    """Produce a human-readable M-Schema block from a datasource adapter.

    Args:
        adapter: A connected ``DatasourceAdapter`` (SQLite or Postgres).
        allowed_tables: Optional list of table names to include. If ``None``, all tables.
        sample_rows: Number of distinct values to sample per low-cardinality TEXT column.
                     Set to 0 to skip sampling (faster).
        sample_budget_ms: Total wall-clock budget for value sampling. Sampling
                     stops once this budget is exceeded (0 disables the budget).

    Returns:
        A multi-line string describing each table's columns, types, PK markers,
        and (optionally) example values for TEXT columns.
    """
    try:
        schema = adapter.refresh_schema()
    except Exception:
        schema = {}

    if not schema:
        return ""

    if allowed_tables:
        allowed_set = set(allowed_tables)
        schema = {t: cols for t, cols in schema.items() if t in allowed_set}

    # Sample distinct values for low-cardinality TEXT columns. Unbounded blob
    # columns (longtext/json/free-form notes) are skipped: they are both
    # expensive to ``DISTINCT`` over and useless as M-Schema examples.
    samples: dict[str, dict[str, list[str]]] = {}
    if sample_rows > 0:
        q = getattr(adapter, 'quote_char', '"')
        deadline = (
            time.monotonic() + (sample_budget_ms / 1000.0)
            if sample_budget_ms > 0
            else None
        )
        for table, cols in schema.items():
            for col in cols:
                if deadline is not None and time.monotonic() > deadline:
                    logger.info(
                        "render_m_schema: sampling budget exhausted at %s.%s",
                        table, col.name,
                    )
                    break
                if col.dtype.upper() in _SAMPLEABLE_TYPES and not _is_blob_column(col):
                    try:
                        result = adapter.query(
                            f'SELECT DISTINCT {q}{col.name}{q} FROM {q}{table}{q}',
                            row_limit=sample_rows,
                            timeout_ms=1000,
                        )
                        vals = [r[0] for r in result.rows if r and r[0] is not None]
                        if vals:
                            samples.setdefault(table, {})[col.name] = vals
                    except Exception:
                        pass
            else:
                continue
            # Inner loop broke on budget — stop the outer loop too.
            if deadline is not None and time.monotonic() > deadline:
                break

    lines: list[str] = []
    for table_name, cols in sorted(schema.items()):
        lines.append(f"# Table: {table_name}")
        for c in cols:
            tag_parts = []
            if c.is_pk:
                tag_parts.append("PK")
            tag = f" [{'|'.join(tag_parts)}]" if tag_parts else ""
            ex = samples.get(table_name, {}).get(c.name)
            ex_str = f", examples: {ex!r}" if ex else ""
            lines.append(f"  ({c.name}: {c.dtype}{tag}{ex_str})")
        lines.append("")

    return "\n".join(lines)
