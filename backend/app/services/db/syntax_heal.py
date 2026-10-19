"""Syntax self-healing for reserved-word aliases (MySQL family).

Failure class (measured live): the NL2SQL model writes
``COUNT(*) AS lines`` — ``lines`` is a MySQL 8 reserved word — and the
statement dies with ER 1064 before returning rows. The catalog cannot carry
the target dialect's reserved-word grammar, so the pipeline must HEAL the
statement, exactly like the collation-mismatch heal (ER 1267) beside which
it runs.

Healing strategy (MySQL/MariaDB only — 1064 alias conflicts are a
MySQL-class symptom):
    1. The database's OWN error message names the offending token
       (pymysql: "... near 'lines FROM ...'"). That token is primary
       evidence, so it is healed even if it is not on any word list.
    2. EVERY unquoted ``AS <identifier>`` alias in the statement that is a
       reserved word (or the offending token) is wrapped in backticks in
       ONE pass — one retry must fix the whole statement, not just the
       first token the engine choked on.

Already-quoted aliases (``AS `lines` ``) are never touched, and identifiers
in non-alias positions are never rewritten (quoting a table name or column
reference is a different decision with different risk).

One healing attempt per execution (the caller retries once); the healed
statement is provenance (``validation["syntax_healed"]``).

Other dialects: PostgreSQL/SQLite/MSSQL raise different syntax-error shapes
with different quoting rules — this module deliberately returns None for
them rather than guessing.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_SYNTAX_ERROR_MARKERS = (
    "error in your sql syntax",
    "syntax error",
)

# Common MySQL 8 reserved words that appear as ALIASES in generated SQL.
# This is the dialect's grammar, not domain vocabulary — the same list
# applies to every MySQL warehouse. Identifiers the engine itself names in
# the error's near-text are healed regardless of this list.
_RESERVED_ALIAS_WORDS = frozenset({
    "lines", "rows", "rank", "groups", "system", "except", "intersect",
    "lateral", "recursive", "empty", "leading", "trailing", "of", "unique",
    "when", "while", "await", "cume_dist", "dense_rank", "first_value",
    "grouping", "lag", "last_value", "lead", "nth_value", "ntile",
    "percent_rank", "row_number", "stored", "virtual", "window",
})

_NEAR_RE = re.compile(r"near\s+'([^']*)'", re.IGNORECASE)
_ALIAS_RE = re.compile(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.IGNORECASE)
_FIRST_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def is_syntax_error(exc: BaseException | str) -> bool:
    """True when the exception text indicates a SQL syntax error (1064).
    Never raises; deliberately broad for translated driver messages."""
    text = str(exc or "").lower()
    if not text:
        return False
    if "1064" in text:
        return True
    return any(marker in text for marker in _SYNTAX_ERROR_MARKERS)


def _offending_tokens(exc: BaseException | str) -> set[str]:
    """Identifiers the engine itself blamed, lowercased.

    pymysql's near-text STARTS at the offending token, so the first word of
    the fragment is the strongest single piece of evidence we have.
    """
    tokens: set[str] = set()
    for near in _NEAR_RE.findall(str(exc or "")):
        word = _FIRST_WORD_RE.search(near or "")
        if word:
            tokens.add(word.group(0).lower())
    return tokens


def heal_syntax(sql: str, exc: BaseException | str, dialect: str) -> str | None:
    """Heal reserved-word aliases, or None when this error is not healable.

    Driven by the engine's near-text token + alias-position analysis.
    """
    if (dialect or "").lower() not in ("mysql", "mariadb"):
        return None
    if not sql or not is_syntax_error(exc):
        return None

    blame = _offending_tokens(exc)

    def _needs_quoting(alias: str) -> bool:
        low = alias.lower()
        return low in blame or low in _RESERVED_ALIAS_WORDS

    healed = _ALIAS_RE.sub(
        lambda m: (
            f"AS `{m.group(1)}`" if _needs_quoting(m.group(1))
            else m.group(0)
        ),
        sql,
    )
    if healed == sql:
        return None

    # Guard: only accept rewrites that actually changed alias positions.
    logger.warning(
        "syntax_heal: quoted reserved-word alias(es) after ER 1064 "
        "(%s…)", str(exc)[:160],
    )
    return healed
