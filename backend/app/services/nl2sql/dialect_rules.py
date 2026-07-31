"""Per-dialect quoting rule hints injected into the LLM SQL-generation prompt.

Each rule tells the LLM how to quote identifiers (table/column names) so
that reserved words and mixed-case names don't cause syntax errors.
"""

from __future__ import annotations

_RULES: dict[str, str] = {
    "postgres": (
        "Quoting rule: PostgreSQL uses double-quotes for identifiers. "
        'Example: SELECT "name" FROM "users". '
        "Always quote identifiers that are reserved words (e.g. order, group, select) "
        "or contain uppercase/special characters. "
        "Aliases do not need quotes unless they are reserved words."
    ),
    "postgresql": (
        "Quoting rule: PostgreSQL uses double-quotes for identifiers. "
        'Example: SELECT "name" FROM "users". '
        "Always quote identifiers that are reserved words (e.g. order, group, select) "
        "or contain uppercase/special characters. "
        "Aliases do not need quotes unless they are reserved words."
    ),
    "sqlite": (
        "Quoting rule: SQLite accepts double-quotes for identifiers. "
        'Example: SELECT "name" FROM "users". '
        "Square brackets [column] and backticks `column` are also supported for compatibility. "
        "Prefer double-quotes for portability across dialects."
    ),
    "mysql": (
        "Quoting rule: MySQL uses backticks for identifiers. "
        "Example: SELECT `name` FROM `users`. "
        "Always quote identifiers that are reserved words (e.g. order, group, select) "
        "or contain uppercase/special characters."
    ),
}


def quote_rule(dialect: str) -> str:
    """Return a human-readable quoting hint for the given dialect.

    Args:
        dialect: Lower-case dialect name (``"postgres"``, ``"sqlite"``, ``"mysql"``, etc.).

    Returns:
        A one-line string suitable for appending to the LLM prompt.
    """
    key = dialect.lower()
    return _RULES.get(key, _RULES["postgres"])
