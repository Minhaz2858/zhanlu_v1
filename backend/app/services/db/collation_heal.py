"""Collation-mismatch self-healing for cross-database joins (dialect-aware).

Failure class (measured live on the EcoPolymer warehouse): a query joining
two MySQL tables with different column collations fails with error 1267
("Illegal mix of collations ... utf8mb4_general_ci vs utf8mb4_0900_ai_ci")
before returning any rows. The NL2SQL planner cannot know column collations
(the catalog doesn't carry them), so the pipeline must HEAL the statement.

Healing strategy (MySQL family only — collation-mismatch is a MySQL-class
error; other engines raise different symptoms):
    rewrite every cross-table column-vs-column equality in JOIN ON
    conditions and WHERE clauses as
        CONVERT(a.col USING utf8mb4) = CONVERT(b.col USING utf8mb4)
    which normalizes both sides to one collation and lets the database
    execute the join. Only cross-table predicates are rewritten —
    column-vs-literal and same-table comparisons are left untouched (they
    never trip 1267, and rewriting them could break index usage).

One healing attempt per execution (callers retry once); the healed
statement is recorded in provenance (``executed_sql`` +
``validation["collation_healed"]``) so auditors see what actually ran.

Other dialects: PostgreSQL/SQLite/MSSQL do not raise this error class —
``is_collation_error`` still detects the text (for hosted engines that
borrow MySQL semantics) but ``heal_join_collations`` only rewrites for
MySQL/MariaDB.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_COLLATION_ERROR_MARKERS = (
    "illegal mix of collations",
    "collation mismatch",
)

_CHARSET_BY_DIALECT = {
    "mysql": "utf8mb4",
    "mariadb": "utf8mb4",
}


def is_collation_error(exc: BaseException | str) -> bool:
    """True when the exception text indicates a collation-mismatch failure.
    Never raises; deliberately broad (covers translated driver messages)."""
    text = str(exc).lower()
    if not text:
        return False
    if "collation" in text:
        return True
    return "1267" in text  # MySQL ER 1267: ER_COLLATION_CHARSET_MISMATCH


def _target_charset(dialect: str) -> str | None:
    return _CHARSET_BY_DIALECT.get((dialect or "").lower())


def heal_join_collations(
    sql: str, dialect: str = "mysql", charset: str | None = None
) -> str | None:
    """Rewrite cross-table join equalities to normalize collations.

    Returns the healed SQL, or ``None`` when healing does not apply
    (unsupported dialect, unparseable SQL, nothing rewritten). Never raises.
    """
    target = charset or _target_charset(dialect)
    if not target or not (sql or "").strip():
        return None
    try:
        import sqlglot
        from sqlglot import exp

        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except Exception as e:  # noqa: BLE001 — unparseable SQL is not healable
        logger.debug("collation_heal: parse failed: %s", e)
        return None

    changed = 0

    def _convert_wrap(col: exp.Column) -> exp.Expression:
        """CAST(col AS CHAR CHARACTER SET <charset>) — MySQL's normalizing
        form that survives sqlglot round-trips (CONVERT(x USING cs) parses
        INTO this same CAST shape)."""
        template = sqlglot.parse_one(
            f"CAST(_zhanlu_ph AS CHAR CHARACTER SET {target})", dialect=dialect
        )
        placeholder = template.find(exp.Column)
        if placeholder is not None:
            placeholder.replace(col.copy())
        return template

    def _rewrite_eq(node: exp.Expression) -> exp.Expression:
        nonlocal changed
        if not isinstance(node, exp.EQ):
            return node
        left, right = node.this, node.expression
        # Only cross-table column-vs-column comparisons trip ER 1267.
        if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
            return node
        lt = (left.table or "").lower()
        rt = (right.table or "").lower()
        if not lt or not rt or lt == rt:
            return node  # same table — no mismatch possible
        node.set("this", _convert_wrap(left))
        node.set("expression", _convert_wrap(right))
        changed += 1
        return node

    try:
        for join in parsed.find_all(exp.Join):
            on = join.args.get("on")
            if on is not None:
                # transform() returns a NEW tree — reattach it, or the
                # mutation is lost (measured: rendered SQL unchanged).
                join.set("on", on.transform(_rewrite_eq))
        where = parsed.args.get("where")
        if where is not None:
            parsed.set("where", where.transform(_rewrite_eq))
    except Exception as e:  # noqa: BLE001
        logger.debug("collation_heal: rewrite failed: %s", e)
        return None

    if not changed:
        return None  # nothing to heal — caller keeps the original error
    try:
        healed = parsed.sql(dialect=dialect)
        logger.info(
            "collation_heal: rewrote %d cross-table join predicate(s) "
            "with CAST(… AS CHAR CHARACTER SET %s)",
            changed, target,
        )
        return healed
    except Exception as e:  # noqa: BLE001
        logger.debug("collation_heal: regeneration failed: %s", e)
        return None


__all__ = ["is_collation_error", "heal_join_collations"]
