"""Does the SQL that ran actually answer the question that was asked?

The data path already guards a lot: freshness short-circuit, TOC table
selection, static/domain routing, structural SQL validation, fan-out guard,
DB-error repair (``NL2SQL_DB_ERROR_REPAIR_MAX``), and a 0-row/degenerate
re-selection loop. What NONE of those check is the thing a business user cares
about: whether a *successful* query answered the question. A query that returns
40 rows from the wrong period, or one summary row for a question that asked for
a trend, is executed, narrated, and presented as authoritative — the failure
mode is silent, and it is the one that erodes trust fastest.

This module adds that check. It is deterministic (no LLM), and deliberately
conservative: each rule fires only on a pattern that is unambiguously wrong, so
a green verdict means "no contradiction found", never "verified".

Rules
-----
``period_not_applied``
    The question names a period ("last month", "2026-08", "上月") but the SQL
    contains no date predicate at all. The strongest signal available: the
    answer is the all-time figure presented as the period figure.
``grain_mismatch``
    The question asks for a breakdown / trend / ranking ("by customer",
    "trend", "top 10", "按", "趋势") but the SQL has no GROUP BY — the answer
    is a single scalar where a series was requested.
``result_truncated``
    The row count equals the execution cap, so the answer is built on a prefix
    of the data. Reported, not "fixed": the honest move is to say so or narrow
    the query.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

# "by region" / "per customer" / "trend" / "top 10" / "breakdown" / 按/各/趋势/分布
# "chart" counts: a chart of a single scalar is not a chart, so a chart request
# implies categories or a series. An explicit aggregate cue still suppresses the
# rule (see _AGGREGATE_CUE).
_BREAKDOWN_CUE = re.compile(
    r"\b(?:by|per|each|trend|over\s+time|monthly|daily|weekly|breakdown|distribution"
    r"|split|top\s*\d+|rank(?:ing)?s?|chart|charts|graph|plot|dashboard)\b"
    r"|按|各|趋势|分布|排名|前\s*\d+|图表|图",
    re.I,
)
# A question that asks for one aggregate is NOT a grain mismatch when the SQL
# returns one row.
_AGGREGATE_CUE = re.compile(
    r"\b(?:total|sum|count|how\s+many|average|avg|overall|maximum|minimum|max|min)\b"
    r"|总共|合计|一共|平均|总数",
    re.I,
)
_GROUP_BY = re.compile(r"\bgroup\s+by\b", re.I)
# Date predicates come in three shapes and all three must count, or the period
# rule produces false positives on correct SQL:
#   1. a literal            FDATE >= '2026-08-01'
#   2. a date function      DATE_FORMAT(CURRENT_DATE - INTERVAL 1 MONTH, ...)
#   3. interval arithmetic  CURDATE() - INTERVAL 30 DAY
# Shape 2 is what the live model actually writes (verified 2026-09-18 against
# demo_warehouse_prod) — missing it flagged a CORRECT query as unfiltered.
_DATE_LITERAL = r"\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?"
_DATE_FUNC = (
    r"(?:date_format|str_to_date|date_sub|date_add|date_trunc|to_date|last_day"
    r"|from_unixtime|unix_timestamp|extract|year|month|day|datetime|timestamp"
    r"|curdate|current_date|current_timestamp|now\s*\(|sysdate|getdate|date\s*\()"
)
_DATE_PREDICATE = re.compile(
    rf"(?:>=|<=|>|<|=|between)\s*\(?\s*(?:['\"]?{_DATE_LITERAL}|{_DATE_FUNC})"
    rf"|['\"]{_DATE_LITERAL}['\"]"
    rf"|\binterval\s+\d+"
    rf"|{_DATE_FUNC}[^)]*\)\s*(?:>=|<=|>|<|=|between)",
    re.I,
)
_YEAR_ONLY = re.compile(r"20\d{2}")


@dataclass
class ContractReport:
    """Result of checking one (question, sql, rows) triple."""

    violations: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.violations

    def feedback(self, candidate_dimensions: list[str] | None = None) -> str:
        """Instruction the SQL model can act on — naming WHAT to group by.

        A generic "add a GROUP BY" gets ignored; handing over the actual
        dimension columns of the selected tables is what lets the retry land.
        """
        parts = []
        if "period_not_applied" in self.violations:
            window = self.detail.get("window")
            parts.append(
                f"the question asks about {self.detail.get('period_phrase') or 'a specific period'}"
                + (f" ({window[0]}..{window[1]})" if window else "")
                + ", but the SQL has no date filter — it would return the all-time figure"
            )
        if "grain_mismatch" in self.violations:
            hint = ""
            dims = [d for d in (candidate_dimensions or []) if d]
            if dims:
                hint = (
                    " — group by the dimension the question names, e.g. GROUP BY "
                    + ", ".join(str(d) for d in dims[:4])
                )
            parts.append(
                "the question asks for a breakdown/trend but the SQL has no GROUP BY, "
                f"so it returns a single aggregate row{hint}"
            )
        if "result_truncated" in self.violations:
            parts.append(
                "the result hit the row cap, so the answer covers only the first rows — "
                "aggregate or narrow the query instead of listing raw rows"
            )
        return "The previous SQL does not satisfy the request: " + "; ".join(parts) + "."


def question_period(question: str, today: Optional[date] = None) -> tuple[tuple[date, date], str] | None:
    """Resolve the period a question names, if any.

    Relative phrases come from the shared resolver in
    ``knowledge_graph.business_context`` (one parser, no second dialect), with a
    narrow addition for an explicit ``YYYY-MM`` month.
    """
    q = question or ""
    try:
        from app.services.knowledge_graph.business_context import parse_relative_window

        window = parse_relative_window(q, today)
        if window:
            return window, "relative window"
    except Exception:
        pass

    m = re.search(r"(20\d{2})[-/年](\d{1,2})月?", q)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12:
            start = date(year, month, 1)
            end = date(year + (month == 12), (month % 12) + 1, 1)
            from datetime import timedelta

            return (start, end - timedelta(days=1)), m.group(0)
    return None


def sql_has_date_predicate(sql: str) -> bool:
    """True when the SQL narrows on a date literal or a date function."""
    return bool(_DATE_PREDICATE.search(sql or ""))


def sql_is_grouped(sql: str) -> bool:
    return bool(_GROUP_BY.search(sql or ""))


def dimension_candidates(schema_info: dict | None, limit: int = 6) -> list[str]:
    """Groupable columns of the selected tables — what a grouped retry should use.

    Prefers the catalog's own semantic typing (``dimension`` / ``flag``, or a
    column carrying enum values) over name guessing, so this stays DB-agnostic.
    """
    scored: list[tuple[int, str]] = []
    for table in (schema_info or {}).get("tables", []) or []:
        for col in (table.get("columns") or []):
            if not isinstance(col, dict):
                continue
            name = col.get("name") or col.get("column_name") or ""
            if not name:
                continue
            stype = (col.get("semantic_type") or "").lower()
            score = 0
            if stype in ("dimension", "flag"):
                score += 3
            if col.get("enum_values"):
                score += 2
            if re.search(r"(status|state|type|category|group|region|owner|name|code)", name, re.I):
                score += 1
            if score:
                scored.append((score, name))
    scored.sort(key=lambda p: (-p[0], p[1]))
    out: list[str] = []
    for _score, name in scored:
        if name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out


def check_answer_contract(
    question: str,
    sql: str,
    rows: list | None = None,
    *,
    row_cap: int | None = None,
    truncated: bool = False,
) -> ContractReport:
    """Deterministically check that ``sql`` could answer ``question``.

    ``truncated`` comes from the executor (QueryService reports it), which is
    more reliable than re-deriving it from a row count.
    """
    report = ContractReport()
    sql = sql or ""

    period = question_period(question)
    if period is not None:
        window, phrase = period
        if not sql_has_date_predicate(sql):
            report.violations.append("period_not_applied")
            report.detail["period_phrase"] = phrase
            report.detail["window"] = (
                window[0].isoformat() if window else None,
                window[1].isoformat() if window else None,
            )

    if _BREAKDOWN_CUE.search(question or "") and not _AGGREGATE_CUE.search(question or ""):
        if not sql_is_grouped(sql):
            report.violations.append("grain_mismatch")

    if truncated or (row_cap and isinstance(rows, list) and len(rows) >= row_cap):
        report.violations.append("result_truncated")
        report.detail["row_cap"] = row_cap

    return report
