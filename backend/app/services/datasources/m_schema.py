"""M-Schema renderer — produce a <m-schema> text block the LLM can read.

Used by the NL2SQL prompt. Format mirrors the BIRD / Spider M-Schema convention
so the LLM sees table comment, columns (name, type, comment, PK, examples).
"""

from __future__ import annotations

import logging

from app.services.datasources import DatasourceAdapter

logger = logging.getLogger(__name__)


def render_m_schema(
    adapter: DatasourceAdapter,
    allowed_tables: list[str] | None = None,
    sample_rows: int = 3,
) -> str:
    """Produce a human-readable M-Schema block from a datasource adapter.

    Args:
        adapter: A connected ``DatasourceAdapter`` (SQLite or Postgres).
        allowed_tables: Optional list of table names to include. If ``None``, all tables.
        sample_rows: Number of distinct values to sample per low-cardinality TEXT column.
                     Set to 0 to skip sampling (faster).

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

    # Sample distinct values for low-cardinality TEXT columns
    samples: dict[str, dict[str, list[str]]] = {}
    if sample_rows > 0:
        for table, cols in schema.items():
            for col in cols:
                if col.dtype.upper() in ("TEXT", "VARCHAR", "CHARACTER VARYING"):
                    try:
                        result = adapter.query(
                            f'SELECT DISTINCT "{col.name}" FROM "{table}"',
                            row_limit=sample_rows,
                            timeout_ms=1000,
                        )
                        vals = [r[0] for r in result.rows if r and r[0] is not None]
                        if vals:
                            samples.setdefault(table, {})[col.name] = vals
                    except Exception:
                        pass

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
