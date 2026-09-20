"""Per-dialect authoring rules injected into LLM SQL-generation prompts.

This module is now a thin adapter over ``services/db/dialect.py``, which owns
all dialect behavior (quoting, pagination, date functions, null/string
functions, probes, EXPLAIN). Before that layer existed this file held a
49-line dict of *quoting* hints for four dialects and returned the PostgreSQL
text for anything else — so an Oracle or SQL Server source was told to quote
like PostgreSQL, and was told nothing at all about ``LIMIT`` (which those two
dialects reject).

Keeping one adapter means the prompt and the runtime cannot disagree about a
dialect again: ``quote_rule("oracle")`` describes exactly the syntax
``profile_for("oracle")`` will execute.
"""

from __future__ import annotations

from app.services.db.dialect import (
    UNKNOWN_DIALECT_HINT,
    maybe_profile,
)


def quote_rule(dialect: str) -> str:
    """Return the dialect authoring rules for the SQL-generation prompt.

    Args:
        dialect: Lower-case dialect name (``"mysql"``, ``"postgres"``,
            ``"postgresql"``, ``"sqlite"``, ``"mssql"``, ``"oracle"`` …).

    Returns:
        A rules block covering identifier quoting, row capping, date/timestamp
        expressions, null and string functions, window-function support and
        query plans.

    Unknown dialects do NOT fall back to another vendor's rules. They get an
    explicit statement that the dialect is unidentified plus the ANSI-standard
    constraints to author against — the old behaviour (silently describing
    PostgreSQL) is how a wrong-quoting prompt reached an Oracle source.
    """
    profile = maybe_profile(dialect)
    if profile is None:
        return UNKNOWN_DIALECT_HINT
    return profile.prompt_hint()
