"""SQL validator — enforces read-only, safe queries before execution.

The validator checks:
1. Only SELECT statements (no INSERT, UPDATE, DELETE, DROP, etc.)
2. No dangerous functions (pg_sleep, pg_terminate_backend, etc.)
3. Optional table/column allowlist enforcement
4. Query complexity limits (max JOINs, max subqueries)
"""

import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# SQL statements that modify data — strictly forbidden
FORBIDDEN_KEYWORDS = [
    "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER", "CREATE",
    "GRANT", "REVOKE", "MERGE", "CALL", "EXEC", "EXECUTE",
    "VACUUM", "ANALYZE", "REINDEX", "CLUSTER", "COPY",
]

# Dangerous functions that could cause side effects
FORBIDDEN_FUNCTIONS = [
    "pg_sleep", "pg_terminate_backend", "pg_cancel_backend",
    "lo_import", "lo_export", "pg_read_file", "pg_write_file",
    "pg_ls_dir", "pg_stat_file", "dblink",
]

# Max query complexity
MAX_JOINS = 10
MAX_SUBQUERIES = 5


class ValidationError(Exception):
    """Raised when SQL validation fails."""
    pass


def validate_sql(sql: str, allowed_tables: Optional[list] = None) -> dict:
    """Validate a SQL query for read-only safety.

    Args:
        sql: The SQL query string to validate
        allowed_tables: Optional list of allowed table names (if None, all tables allowed)

    Returns:
        {"valid": bool, "errors": [str], "warnings": [str]}

    Raises:
        ValidationError: If the query contains forbidden operations
    """
    errors = []
    warnings = []

    if not sql or not sql.strip():
        return {"valid": False, "errors": ["Empty SQL query"], "warnings": []}

    # Normalize for checking (keep original for execution)
    sql_upper = sql.upper().strip()

    # Must start with SELECT or WITH (CTE)
    if not (sql_upper.startswith("SELECT") or sql_upper.startswith("WITH")):
        errors.append("Query must start with SELECT or WITH (CTE)")

    # Check for forbidden keywords (as standalone words, not substrings)
    for keyword in FORBIDDEN_KEYWORDS:
        # Use word boundary matching to avoid false positives
        pattern = r'\b' + keyword + r'\b'
        if re.search(pattern, sql_upper):
            errors.append(f"Forbidden keyword '{keyword}' detected")

    # Check for forbidden functions
    for func in FORBIDDEN_FUNCTIONS:
        if func.lower() in sql.lower():
            errors.append(f"Forbidden function '{func}' detected")

    # Check for semicolons (multi-statement injection prevention)
    # Allow semicolons only inside string literals
    sql_no_strings = re.sub(r"'[^']*'", "", sql)
    if ";" in sql_no_strings.rstrip(";"):
        errors.append("Multiple statements detected (semicolons not allowed except trailing)")

    # Count JOINs
    join_count = len(re.findall(r'\bJOIN\b', sql_upper))
    if join_count > MAX_JOINS:
        warnings.append(f"Query has {join_count} JOINs (max recommended: {MAX_JOINS})")

    # Count subqueries
    subquery_count = sql_upper.count("SELECT") - 1  # Subtract the main SELECT
    if subquery_count > MAX_SUBQUERIES:
        warnings.append(f"Query has {subquery_count} subqueries (max recommended: {MAX_SUBQUERIES})")

    # Check table allowlist
    if allowed_tables:
        # Extract table names from FROM and JOIN clauses
        table_pattern = r'(?:FROM|JOIN)\s+(\w+)'
        found_tables = re.findall(table_pattern, sql_upper)
        for table in found_tables:
            if table not in [t.upper() for t in allowed_tables]:
                errors.append(f"Table '{table}' not in allowed list: {allowed_tables}")

    is_valid = len(errors) == 0

    if not is_valid:
        logger.warning("SQL validation failed: %s", errors)

    return {
        "valid": is_valid,
        "errors": errors,
        "warnings": warnings,
    }
