"""Business Semantic Layer — business context + freshness helpers.

Pure, deterministic helpers that sit on top of the structural catalog:

- :func:`parse_relative_window` — resolve "last N days", "this week", "YTD"
  (en/zh) into a ``(start, end)`` date window without an LLM. Dialect-agnostic:
  returns resolved ISO dates, never a Postgres-only ``INTERVAL`` expression.
- :func:`freshness_verdict` — decide whether a question's window is entirely
  after every relevant table's ``coverage_json.max_date``.
- :func:`build_business_context` — assemble a token-capped block of approved
  ``project_metric`` definitions + coverage annotations for prompt injection.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_MAX_CONTEXT_TOKENS = 500
_APPROX_CHARS_PER_TOKEN = 3.5


# ── relative-date parsing ──────────────────────────────────────────────────

_LAST_N_DAYS_RE = re.compile(
    r"(?:last|past|recent|近|最近|过去|过去)\s*(\d+)\s*(?:day|days|日|天)", re.I
)
_LAST_N_WEEKS_RE = re.compile(
    r"(?:last|past|recent|近|最近|过去|过去)\s*(\d+)\s*(?:week|weeks|周|星期|週)", re.I
)
_LAST_N_MONTHS_RE = re.compile(
    r"(?:last|past|recent|近|最近|过去|过去)\s*(\d+)\s*(?:month|months|月)", re.I
)
_THIS_WEEK_RE = re.compile(r"(?:this\s+week|本周|这周|这週|本周)", re.I)
_LAST_WEEK_RE = re.compile(r"(?:last\s+week|上周|上週|上周)", re.I)
_THIS_MONTH_RE = re.compile(r"(?:this\s+month|本月|这个月|這個月)", re.I)
_LAST_MONTH_RE = re.compile(r"(?:last\s+month|上月|上个月|上個月)", re.I)
_THIS_YEAR_RE = re.compile(r"(?:this\s+year|今年|今年)", re.I)
_LAST_YEAR_RE = re.compile(r"(?:last\s+year|去年|去年)", re.I)
_YTD_RE = re.compile(r"(?:ytd|year[- ]to[- ]date|年初至今|今年至今)", re.I)


def parse_relative_window(question: str, today: date | None = None) -> tuple[date, date] | None:
    """Resolve a relative-time phrase into an inclusive ``(start, end)`` window.

    Returns ``None`` when the question carries no recognizable relative-window
    phrase (caller should fall through to a normal query). Deterministic and
    dialect-agnostic — no SQL generation, no LLM.
    """
    if not question:
        return None
    today = today or date.today()
    q = question

    m = _LAST_N_DAYS_RE.search(q)
    if m:
        n = int(m.group(1))
        return today - timedelta(days=n), today

    m = _LAST_N_WEEKS_RE.search(q)
    if m:
        n = int(m.group(1))
        return today - timedelta(weeks=n), today

    m = _LAST_N_MONTHS_RE.search(q)
    if m:
        n = int(m.group(1))
        start = _shift_months(today, -n)
        return start, today

    if _THIS_WEEK_RE.search(q):
        return today - timedelta(days=today.weekday()), today

    if _LAST_WEEK_RE.search(q):
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=6)

    if _THIS_MONTH_RE.search(q):
        return today.replace(day=1), today

    if _LAST_MONTH_RE.search(q):
        first = today.replace(day=1)
        prev = _shift_months(first, -1)
        return prev, first - timedelta(days=1)

    if _THIS_YEAR_RE.search(q):
        return today.replace(month=1, day=1), today

    if _LAST_YEAR_RE.search(q):
        start = today.replace(year=today.year - 1, month=1, day=1)
        return start, today.replace(month=1, day=1) - timedelta(days=1)

    if _YTD_RE.search(q):
        return today.replace(month=1, day=1), today

    return None


def _shift_months(d: date, months: int) -> date:
    """Shift ``d`` by ``months`` (negative = backwards), clamping day."""
    m = d.month + months
    year = d.year + (m - 1) // 12
    month = (m - 1) % 12 + 1
    day = min(d.day, _days_in_month(year, month))
    return date(year, month, day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    return (nxt - timedelta(days=1)).day


# ── coverage / freshness ───────────────────────────────────────────────────

def _coverage_max_dates(db: Session, kb_id: str) -> list[dict]:
    """Load ``coverage_json`` for all tables of a KB that have one.

    Returns ``[{table_name, max_date, date_column}]``. Raises nothing — returns
    ``[]`` when the model/column is unavailable (flag off / migration pending).
    """
    try:
        from app.models.knowledge_catalog import KBTableMeta
    except Exception:  # pragma: no cover - import guard
        return []
    rows = (
        db.query(KBTableMeta)
        .filter(KBTableMeta.kb_id == kb_id, KBTableMeta.is_deleted.is_(False))
        .all()
    )
    out: list[dict] = []
    for r in rows:
        cov = r.coverage_json or {}
        max_date = cov.get("max_date")
        if not max_date:
            continue
        out.append({
            "table_name": r.table_name,
            "date_column": cov.get("date_column"),
            "max_date": _to_date(max_date),
        })
    return out


def _to_date(value: Any) -> date | None:
    """Best-effort coerce a ``YYYY-MM-DD`` / datetime / ISO string to ``date``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value)[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def freshness_verdict(
    db: Session,
    kb_id: str,
    window: tuple[date, date] | None,
) -> dict | None:
    """Decide whether the question's window is entirely stale.

    Returns ``None`` when there is no window, no coverage data, or at least one
    relevant table still has data within the window. Otherwise returns a dict
    ``{stale: True, max_date, message}`` with the latest observed date.
    """
    if not window:
        return None
    covs = _coverage_max_dates(db, kb_id)
    if not covs:
        return None
    start, _end = window
    dated = [c for c in covs if c["max_date"] is not None]
    if not dated:
        return None

    latest = max(c["max_date"] for c in dated)
    # Stale only when EVERY probed table's max_date precedes the window start.
    if all(c["max_date"] < start for c in dated):
        return {
            "stale": True,
            "max_date": latest.isoformat(),
            "message": (
                f"There is no data after {latest.isoformat()} for this data "
                f"source, but the question asks about a period starting "
                f"{start.isoformat()}. The data is stale — the ERP/warehouse "
                f"sync may be broken. The most recent available date is "
                f"{latest.isoformat()}."
            ),
        }
    return None


# ── business context injection ─────────────────────────────────────────────

def _match_metrics(project_id: str, kb_id: str, question: str, db: Session) -> list:
    """Return approved metrics whose aliases overlap the question.

    Matching is token-overlap / substring, CJK-aware (substring matches work
    for Chinese without tokenization). Only ``status='approved'`` rows are
    returned — proposed metrics are never injected.
    """
    try:
        from app.models.knowledge_catalog import ProjectMetric
    except Exception:  # pragma: no cover - import guard
        return []

    q = (question or "").lower()
    rows = (
        db.query(ProjectMetric)
        .filter(
            ProjectMetric.project_id == project_id,
            ProjectMetric.status == "approved",
            ProjectMetric.is_deleted.is_(False),
        )
        .all()
    )
    if kb_id:
        rows = [r for r in rows if not r.kb_id or r.kb_id == kb_id]

    matched = []
    for r in rows:
        haystacks = [r.name or ""] + [a for a in (r.aliases or []) if a]
        if _overlaps(q, haystacks):
            matched.append(r)
    return matched


def _overlaps(q: str, haystacks: list[str]) -> bool:
    q = q.lower()
    for h in haystacks:
        h = h.lower()
        if not h:
            continue
        if h in q or q in h:
            return True
        # CJK: compare 2-gram substrings so "毛利率" matches "毛利率是多少".
        for tok in _ngrams(q, 2):
            if tok and tok in h:
                return True
    return False


def _ngrams(s: str, n: int):
    if len(s) < n:
        return [s]
    return [s[i:i + n] for i in range(len(s) - n + 1)]


def build_business_context(
    db: Session,
    project_id: str,
    kb_id: str,
    question: str,
) -> str:
    """Assemble a token-capped business-context block for prompt injection.

    Progressive disclosure: only matched approved metric definitions plus the
    KB's coverage annotations are included — never the whole registry. Returns
    ``""`` when there is nothing to inject (flag off / no approved metrics).
    """
    from app.config import settings

    if not settings.KG_BUSINESS_CONTEXT_ENABLED:
        return ""

    parts: list[str] = []

    # 1. Matched approved metrics.
    metrics = _match_metrics(project_id, kb_id, question, db)
    if metrics:
        lines = ["## Business metrics (approved)"]
        for m in metrics:
            seg = [f"- {m.name}"]
            if m.unit:
                seg.append(f" ({m.unit})")
            if m.definition:
                seg.append(f": {m.definition}")
            if m.sql_expression:
                seg.append(f" [SQL: {m.sql_expression}]")
            if m.query_pattern:
                seg.append(f" [pattern: {m.query_pattern}]")
            if m.default_aggregation:
                seg.append(f" [agg: {m.default_aggregation}]")
            lines.append("".join(seg))
        parts.append("\n".join(lines))

    # 2. Coverage annotations.
    covs = _coverage_max_dates(db, kb_id)
    if covs:
        lines = ["## Data coverage"]
        for c in covs:
            col = c["date_column"] or "date"
            md = c["max_date"].isoformat() if c["max_date"] else "unknown"
            lines.append(f"- {c['table_name']}.{col}: latest {md}")
        parts.append("\n".join(lines))

    block = "\n\n".join(parts).strip()
    if not block:
        return ""

    # Token cap (approx by chars).
    if len(block) > _MAX_CONTEXT_TOKENS * _APPROX_CHARS_PER_TOKEN:
        block = block[: int(_MAX_CONTEXT_TOKENS * _APPROX_CHARS_PER_TOKEN)]
        block = block.rsplit("\n", 1)[0] + "\n(truncated)"
    return block
