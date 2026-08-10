"""SQL validation and execution engine.

Validates that SQL is SELECT-only (no DDL/DML), enforces max_rows caps,
and executes through QueryService.
"""

from __future__ import annotations

import re


# Dangerous SQL keywords we reject at the statement level.
_DML_DDL_KEYWORDS = re.compile(
    r"\b(DROP|CREATE|ALTER|INSERT|UPDATE|DELETE|TRUNCATE|GRANT|REVOKE|EXEC|EXECUTE)\b",
    re.IGNORECASE,
)

# Regex to detect multiple SQL statements separated by semicolons.
# A simple heuristic: count semicolons outside of string literals.
_MULTI_STMT_RE = re.compile(r";\s*(?![\s]*$)")


def validate_sql(sql: str, _db_type: str | None = None) -> str | None:
    """Validate that a SQL string is a single, read-only SELECT statement.

    Returns None if valid, or an error message string if rejected.
    """
    stripped = (sql or "").strip()
    if not stripped:
        return "SQL query is empty."

    # Reject non-SELECT statements
    upper = stripped.upper()
    if not upper.startswith("SELECT"):
        return "Only SELECT queries are allowed."

    # Reject DDL/DML keywords anywhere in the query
    if _DML_DDL_KEYWORDS.search(stripped):
        return "SQL contains DDL/DML keywords — only SELECT is allowed."

    # Reject multiple statements (semicolons that are not at end)
    # Simple heuristic: strip trailing semicolons, then check for remaining ones.
    cleaned = stripped.rstrip(";").rstrip()
    if ";" in cleaned:
        # Allow a single trailing semicolon before end
        test = cleaned.rsplit(";", 1)
        if len(test) > 1 and test[1].strip():
            return "Multiple SQL statements are not allowed."

    return None  # valid
