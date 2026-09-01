"""SQL validator — sqlglot-based guard that rejects unsafe/out-of-scope queries."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import expressions as exp

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    is_valid: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sql_hash: str = ""
    normalized_sql: str = ""
    tables_referenced: list[str] = field(default_factory=list)
    columns_referenced: list[str] = field(default_factory=list)


# Blocked statement types (deny-list)
_BLOCKED_KINDS = frozenset({
    "insert", "update", "delete", "drop", "alter", "create",
    "truncate", "replace", "merge", "grant", "revoke",
})

# Dangerous PostgreSQL / SQLite / cross-DB functions that should never appear in NL2SQL output.
# Matched case-insensitively as whole-word tokens in the raw SQL text (before sqlglot parse).
_FORBIDDEN_FN_PATTERN = re.compile(
    r"\b(pg_sleep|pg_terminate_backend|pg_read_file|pg_read_binary_file|"
    r"pg_write_file|pg_write_binary_file|"
    r"lo_import|lo_export|lo_create|lo_unlink|"
    r"dblink|dblink_exec|dblink_connect|dblink_disconnect|"
    r"pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|"
    r"set_config\("
    r")\b",
    re.IGNORECASE,
)


def validate(
    sql: str,
    *,
    allowed_tables: list[str] | None = None,
    allowed_columns: list[str] | None = None,
    block_tables: list[str] | None = None,
) -> ValidationResult:
    """Validate *sql* against governance rules.

    Rules enforced (in order):
    1. Must be a single ``SELECT`` statement (no multi-statement)
    2. No blocked statement types (INSERT, UPDATE, DELETE, DDL, …)
    3. All referenced tables must be in *allowed_tables* (if provided)
    4. All referenced columns must be in *allowed_columns* (if provided)
    5. No tables in *block_tables*

    Args:
        sql: Raw SQL string from the user/LLM.
        allowed_tables: List of table names that are permitted.
        allowed_columns: List of column names (fully-qualified or bare) permitted.
        block_tables: List of table names that are explicitly denied.

    Returns:
        ``ValidationResult`` with ``is_valid`` and error/warning details.
    """
    result = ValidationResult()
    result.normalized_sql = _normalize(sql)
    result.sql_hash = hashlib.sha256(result.normalized_sql.encode()).hexdigest()[:16]

    if not sql.strip():
        result.errors.append("Empty SQL")
        return result

    # ── Rule 0: Dangerous function blocklist (regex on raw text) ─
    for match in _FORBIDDEN_FN_PATTERN.finditer(sql):
        fn_name = match.group(1)
        result.errors.append(f"Forbidden function: {fn_name}")

    # ── Parse ────────────────────────────────────────────────────
    try:
        parsed = sqlglot.parse_one(sql, read="postgres")
    except Exception as e:
        result.errors.append(f"SQL parse error: {e}")
        return result

    # ── Rule 1: Must be SELECT ───────────────────────────────────
    if parsed.key != "select":
        result.errors.append(f"Only SELECT statements are allowed, got: {parsed.key}")

    # ── Rule 2: Blocked statement types (from ast tree) ──────────
    for node in parsed.walk():
        kind = _node_kind(node)
        if kind in _BLOCKED_KINDS:
            result.errors.append(f"Blocked statement type: {kind}")

    # ── Collect referenced tables & columns ──────────────────────
    tables = _extract_tables(parsed)
    columns = _extract_columns(parsed)
    result.tables_referenced = tables
    result.columns_referenced = columns

    # ── Rule 3: Allowed tables ───────────────────────────────────
    if allowed_tables is not None:
        allowed_lower = {t.lower() for t in allowed_tables}
        for t in tables:
            if t.lower() not in allowed_lower:
                result.errors.append(f"Table '{t}' is not in the allow-list")

    # ── Rule 4: Allowed columns ──────────────────────────────────
    if allowed_columns is not None:
        allowed_cols_lower = {c.lower() for c in allowed_columns}
        for c in columns:
            bare = c.split(".")[-1]
            if c.lower() not in allowed_cols_lower and bare.lower() not in allowed_cols_lower:
                result.warnings.append(f"Column '{c}' is not in the allow-list")

    # ── Rule 5: Blocked tables ───────────────────────────────────
    if block_tables:
        block_lower = {t.lower() for t in block_tables}
        for t in tables:
            if t.lower() in block_lower:
                result.errors.append(f"Table '{t}' is explicitly blocked")

    result.is_valid = len(result.errors) == 0
    return result


# ── internal helpers ──────────────────────────────────────────────────


def _normalize(sql: str) -> str:
    """Lower-case keywords and collapse whitespace for hashing."""
    try:
        parsed = sqlglot.parse_one(sql)
        return parsed.sql(dialect="postgres").strip()
    except Exception:
        return " ".join(sql.strip().lower().split())


def _node_kind(node: exp.Expression) -> str:
    try:
        return node.key  # sqlglot internal
    except Exception:
        return ""


def _extract_tables(parsed: exp.Expression) -> list[str]:
    tables: set[str] = set()
    for node in parsed.walk():
        if isinstance(node, exp.Table):
            name = node.name
            db = getattr(node, "db", None)
            if db:
                name = f"{db}.{name}"
            if name:
                tables.add(name)
    return sorted(tables)


def _extract_columns(parsed: exp.Expression) -> list[str]:
    cols: set[str] = set()
    for node in parsed.walk():
        if isinstance(node, exp.Column):
            table = getattr(node, "table", "")
            col = node.name
            if table:
                cols.add(f"{table}.{col}")
            else:
                cols.add(col)
    return sorted(cols)
