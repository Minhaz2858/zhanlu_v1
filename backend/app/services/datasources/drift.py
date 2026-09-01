"""Schema-drift detector for datasources.

Compares the cached ``schema_snapshot`` against a freshly-refreshed schema
and reports added / removed columns and type changes.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def detect(
    stored: dict[str, list[dict[str, Any]]],
    live: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Compare two schema dicts and return added, removed, and type-changed columns.

    Args:
        stored: The previously cached schema (from ``Datasource.schema_snapshot``).
        live: Freshly-queried schema (from ``DatasourceAdapter.refresh_schema()``).

    Returns:
        Dict with keys ``added_cols``, ``removed_cols``, ``type_changed``.
        Each value is a list of ``{"table": ..., "column": ...}`` dicts.
    """
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    type_changed: list[dict[str, Any]] = []

    # Build lookup maps: {table: {col_name: dtype}}
    def _index(schema: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, str]]:
        idx: dict[str, dict[str, str]] = {}
        for tbl, cols in schema.items():
            idx[tbl] = {}
            for c in cols:
                idx[tbl][c["name"]] = c.get("dtype", "")
        return idx

    stored_idx = _index(stored)
    live_idx = _index(live)

    # Detect added / type-changed
    for tbl, cols in live_idx.items():
        stored_cols = stored_idx.get(tbl, {})
        for col, dtype in cols.items():
            prev_dtype = stored_cols.get(col)
            if prev_dtype is None:
                added.append({"table": tbl, "column": col})
            elif prev_dtype.upper() != dtype.upper():
                type_changed.append({
                    "table": tbl, "column": col,
                    "old_dtype": prev_dtype, "new_dtype": dtype,
                })

    # Detect removed
    for tbl, cols in stored_idx.items():
        live_cols = live_idx.get(tbl, {})
        for col in cols:
            if col not in live_cols:
                removed.append({"table": tbl, "column": col})

    return {
        "added_cols": added,
        "removed_cols": removed,
        "type_changed": type_changed,
    }
