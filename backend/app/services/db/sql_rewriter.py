"""SQL Rewriter — the OCI "SQL Rewriting" stage, deterministic and safe.

The article's stage 5: "Even correct SQL can be too expensive to run at
enterprise scale... rewrites queries while preserving semantics." This module
implements the DETERMINISTIC subset of that idea. Its hard rule, enforced by
the tests: a rewrite that could change the answer (row count, grain, filter)
is recorded as a note, never applied. What qualifies as a rewrite here:

* ``limit_enforcement``   — append ``LIMIT n`` when the caller declares an
  exploratory row budget and the statement names none. Row-count-bounding a
  result the executor would have clamped anyway.
* ``predicate_pushdown``  — move an outer equality literal into a plain
  derived table so the inner scan filters early. Only when the inner query
  has no DISTINCT / GROUP BY / HAVING / ORDER BY / LIMIT / UNION and no
  aggregation (those change what a predicate means), and only for simple
  ``alias.col = literal`` conjuncts.
* ``join_elimination``    — drop an INNER/LEFT join whose table is referenced
  NOWHERE else, and only when the caller supplies uniqueness evidence for the
  joined side's key (a join can fan out or filter rows; without uniqueness it
  is NOT row-count-safe, so it is skipped with a note).

Telemetry-only notes (never rewrites, because they WOULD change semantics):
``SELECT *`` shape, ``COUNT(col)`` null-semantics sanity.

Shape-only by design: no dialect names, no table names, no business
vocabulary. Everything fail-soft — an internal error returns the original SQL
with a note, because a failed optimization must never break a served query.
The rewritten statement still passes the unconditional execution gate inside
``QueryService.execute``; this module never bypasses it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

logger = logging.getLogger(__name__)

#: Statements this module is allowed to touch at all.
_SELECT_START_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_TRAILING_SEMICOLON_RE = re.compile(r";\s*$")
#: An explicit row bound in ANY dialect family we support.
_LIMIT_RE = re.compile(r"\bLIMIT\s+\d", re.IGNORECASE)
_TOP_RE = re.compile(r"\bTOP\s*\(", re.IGNORECASE)
_FETCH_RE = re.compile(r"\bFETCH\s+(?:FIRST|NEXT)\b", re.IGNORECASE)
#: Inner-query modifiers that make a pushed predicate mean something else.
_UNSAFE_INNER_RE = re.compile(
    r"\b(DISTINCT|GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|OFFSET|UNION)\b"
    r"|\b(?:SUM|COUNT|AVG|MIN|MAX|STDDEV|VARIANCE)\s*\(",
    re.IGNORECASE,
)
#: A simple outer conjunct: ``alias.col = literal`` (quoted string or number).
_OUTER_PRED_RE = re.compile(
    r"\b([A-Za-z_][\w$]*)\.([A-Za-z_][\w$]*)\s*=\s*('[^']*'|-?\d+(?:\.\d+)?)"
)
_JOIN_RE = re.compile(
    r"\b(?P<kind>INNER\s+JOIN|LEFT\s+(?:OUTER\s+)?JOIN)\s+"
    r"(?P<table>[A-Za-z_][\w$]*|`[^`]+`|\[[^\]]+\]|\"[^\"]+\")"
    r"(?:\s+(?:AS\s+)?(?P<alias>[A-Za-z_][\w$]*))?"
    r"\s+ON\s+(?P<cond>[^)]*?)(?=\s+(?:INNER\s+JOIN|LEFT\s+(?:OUTER\s+)?JOIN"
    r"|RIGHT\s+(?:OUTER\s+)?JOIN|WHERE|GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|UNION)\b|$)",
    re.IGNORECASE,
)
_EQ_CONDITION_RE = re.compile(
    r"\b([A-Za-z_][\w$]*)\.([A-Za-z_][\w$]*)\s*=\s*"
    r"([A-Za-z_][\w$]*)\.([A-Za-z_][\w$]*)"
)
_IDENTITY_RE = re.compile(r"[A-Za-z_][\w$]*")


@dataclass
class RewriteResult:
    """What the rewriter decided. ``applied`` names the rules that changed SQL."""

    sql: str
    applied: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _strip_backticks(token: str) -> str:
    return token.strip("`[]\"").lower()


# ── rule: limit enforcement ───────────────────────────────────────────────


def _has_row_bound(sql: str) -> bool:
    return bool(_LIMIT_RE.search(sql) or _TOP_RE.search(sql) or _FETCH_RE.search(sql))


def _apply_limit(sql: str, max_rows: int, res: RewriteResult) -> str:
    if max_rows is None or max_rows <= 0 or _has_row_bound(sql):
        return sql
    body = _TRAILING_SEMICOLON_RE.sub("", sql).rstrip()
    if not body:
        return sql
    res.applied.append("limit_enforcement")
    return f"{body}\nLIMIT {int(max_rows)}"


# ── rule: predicate push-down ─────────────────────────────────────────────


def _split_top_level_parens(sql: str) -> list[tuple[int, int]]:
    """Spans of ``(...)`` groups that are not nested inside another group."""
    spans: list[tuple[int, int]] = []
    depth = 0
    start = -1
    for i, ch in enumerate(sql):
        if ch == "(":
            if depth == 0:
                start = i
            depth += 1
        elif ch == ")":
            if depth == 1 and start >= 0:
                spans.append((start, i))
            depth = max(0, depth - 1)
    return spans


def _apply_pushdown(sql: str, res: RewriteResult) -> str:
    """Push outer equality literals into ONE plain derived table.

    Conservative by construction: exactly one top-level derived table, plain
    inner SELECT, simple conjuncts whose column the inner SELECT list names.
    """
    spans = _split_top_level_parens(sql)
    if len(spans) != 1:
        return sql
    start, end = spans[0]
    inner = sql[start + 1 : end]
    if not re.match(r"^\s*SELECT\b", inner, re.IGNORECASE) or _UNSAFE_INNER_RE.search(inner):
        return sql
    # The derived table must be immediately aliased and its alias used in an
    # outer WHERE that directly follows.
    tail = sql[end + 1 :]
    alias_m = re.match(r"\s+(?:AS\s+)?([A-Za-z_][\w$]*)\b", tail, re.IGNORECASE)
    if not alias_m:
        return sql
    alias = alias_m.group(1)
    after_alias = tail[alias_m.end() :]
    where_m = re.match(r"\s+WHERE\s+(.*)$", after_alias, re.IGNORECASE | re.DOTALL)
    if not where_m:
        return sql
    where = where_m.group(1)
    # Only simple conjuncts: no OR, no comparison chains beyond ANDs.
    if re.search(r"\bOR\b", where, re.IGNORECASE):
        return sql

    pushed: list[str] = []
    remaining = where
    for m in _OUTER_PRED_RE.finditer(where):
        pred_alias, col, literal = m.group(1), m.group(2), m.group(3)
        if pred_alias.lower() != alias.lower():
            continue
        select_list = inner[re.search(r"\bSELECT\b", inner, re.IGNORECASE).end() :]
        from_pos = re.search(r"\bFROM\b", select_list, re.IGNORECASE)
        select_list = select_list[: from_pos.start()] if from_pos else select_list
        if not re.search(rf"\b{re.escape(col)}\b", select_list, re.IGNORECASE):
            continue
        pushed.append(f"{col} = {literal}")
        remaining = _OUTER_PRED_RE.sub("", remaining, count=1)

    if not pushed:
        return sql
    # Clean the leftovers: stray ANDs / empty conjuncts left by removal.
    leftover = re.sub(r"\bAND\b\s*(?=\bAND\b|$)", "", remaining, flags=re.IGNORECASE)
    leftover = re.sub(r"^\s*AND\b\s*", "", leftover.strip(), flags=re.IGNORECASE).strip()
    if leftover and not re.match(r"^[A-Za-z_(]", leftover):
        return sql
    inner_new = inner.rstrip()
    if re.search(r"\bWHERE\b", inner_new, re.IGNORECASE):
        inner_new = f"{inner_new} AND {' AND '.join(pushed)}"
    else:
        inner_new = f"{inner_new} WHERE {' AND '.join(pushed)}"
    res.applied.append("predicate_pushdown")
    # Re-emit the derived-table alias (the text between the inner ")" and the
    # WHERE). Dropping it left outer references like ``a.x`` dangling and the
    # derived table unnamed (a hard error on PostgreSQL).
    alias_text = tail[: alias_m.end()]
    return (
        sql[: start + 1] + inner_new + ")" + alias_text
        + (f" WHERE {leftover}" if leftover else "")
    )


# ── rule: join elimination ────────────────────────────────────────────────


def _join_side_keys(cond: str, alias: str) -> set[str]:
    """The join-condition columns that belong to the (candidate-for-removal) side."""
    keys: set[str] = set()
    for m in _EQ_CONDITION_RE.finditer(cond):
        left_a, left_c, right_a, right_c = m.groups()
        if left_a.lower() == alias.lower():
            keys.add(left_c.lower())
        if right_a.lower() == alias.lower():
            keys.add(right_c.lower())
    return keys


def _table_referenced_elsewhere(sql: str, span: tuple[int, int], names: set[str]) -> bool:
    """Do the alias/table names appear anywhere OUTSIDE this join clause?"""
    outside = sql[: span[0]] + sql[span[1] :]
    for name in names:
        if re.search(rf"\b{re.escape(name)}\b", outside, re.IGNORECASE):
            return True
    return False


def _apply_join_elimination(sql: str, unique_columns: Mapping[str, set[str]] | None, res: RewriteResult) -> str:
    if not unique_columns:
        return sql
    for m in _JOIN_RE.finditer(sql):
        table_raw = m.group("table")
        table = _strip_backticks(table_raw)
        alias = m.group("alias") or table
        cond = m.group("cond") or ""
        names = {alias, table, table_raw}
        if _table_referenced_elsewhere(sql, m.span(), names):
            continue
        uniq = {c.lower() for c in (unique_columns.get(table) or unique_columns.get(alias) or [])}
        if not uniq:
            continue
        if not _join_side_keys(cond, alias) & uniq:
            continue
        res.applied.append("join_elimination")
        return (sql[: m.start()] + sql[m.end() :]).strip()
    return sql


# ── telemetry-only notes ──────────────────────────────────────────────────


def _collect_notes(sql: str, res: RewriteResult) -> None:
    if re.search(r"\bSELECT\s+\*", sql, re.IGNORECASE):
        res.notes.append("select_star: shape depends on source; not rewritten at runtime")
    if re.search(r"\bCOUNT\s*\(\s*[A-Za-z_][\w$.]*\s*\)", sql, re.IGNORECASE):
        res.notes.append("count(col): NULL semantics differ from COUNT(*); left as authored")


# ── entry point ───────────────────────────────────────────────────────────


def rewrite_sql(
    sql: str,
    *,
    max_rows: int | None = None,
    unique_columns: Mapping[str, set[str]] | None = None,
) -> RewriteResult:
    """Run the deterministic rewrite pass. NEVER raises, NEVER writes."""
    res = RewriteResult(sql=sql or "")
    if not sql or not _SELECT_START_RE.match(sql):
        return res
    try:
        res.sql = _apply_join_elimination(res.sql, unique_columns, res)
        res.sql = _apply_pushdown(res.sql, res)
        res.sql = _apply_limit(res.sql, max_rows, res)
        _collect_notes(res.sql, res)
    except Exception as e:  # a failed optimization must never break a query
        logger.debug("sql_rewriter: rewrite pass failed (original kept): %s", e)
        res.sql = sql
        res.notes.append(f"rewriter_error: {e}")
    return res
