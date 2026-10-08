"""Query Log Retrieval — OCI online stage: similar prior questions as grounding.

The article's "Query Log Retrieval": retrieve useful PRIOR QUERIES before
generation starts. Zhanlu already reuses the EXACT question (plan memory,
exact-signature). What was missing is the similar-question retrieval: the
successful telemetry rows (``nl2sql_query_logs``) and the verified plan rows
(``query_plan_memory``) are a corpus of "how this business actually asks",
and a question that resembles a past one should start from that answer's
shape instead of a blank prompt.

Rules this module never breaks:

* **Only successful executions are eligible.** A logged error or a denied
  statement is a warning about an answer, not an example of one.
* **Strictly kb-scoped.** Log rows carry ``datasource_id``; a row from
  another warehouse can never surface here (no cross-org semantic leak).
* **The exact question is excluded** — plan memory owns that path; retrieval
  adds the NEIGHBOURS.
* **Popularity never creates a hit.** Ranking is lexical overlap between the
  question asked and the question logged (shape-only tokens, CJK bigrams via
  ``lexical.tokens``); repeat count only breaks ties. An unrelated popular
  question does not qualify.
* **Bounded scan.** At most ``scan_limit`` recent log rows are read (default
  derived from ``SEMANTIC_STORE_LOG_SCAN_LIMIT``), so the hot path never
  scans unbounded telemetry.
* **Fail-soft and flag-gated.** Any error degrades to "no guidance"; the
  caller injects the block through the same ``extra_guidance`` channel plan
  memory uses.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.services.db.lexical import tokens as lexical_tokens
from app.services.db.plan_memory import question_signature

logger = logging.getLogger(__name__)

#: A hit must cover at least this fraction of the question's tokens to be a
#: NEIGHBOUR rather than noise. Below it, "how is OCI doing" would match
#: "how is the weather".
_MIN_COVERAGE = 0.25
#: Per-entry SQL budget in the prompt block (a shape hint, not the answer).
_MAX_SQL_CHARS = 400
#: Total prompt-block budget.
_MAX_TOTAL_CHARS = 1800


def _settings_value(name: str, default: Any) -> Any:
    try:
        return getattr(settings, name, default)
    except Exception:  # pragma: no cover - settings always importable
        return default


def _row_tokens(text: str | None) -> set[str]:
    return lexical_tokens(text or "")


def retrieve_similar_questions(
    db: Session | None,
    kb_id: str,
    question: str,
    *,
    limit: int = 3,
    scan_limit: int | None = None,
) -> list[dict[str, Any]]:
    """The most similar PRIOR successful questions for this KB, best first.

    Each hit: ``{question, sql, source ('query_log'|'plan_memory'), score,
    outcome, uses}``. Never raises; empty list on any problem.
    """
    try:
        if db is None or not (question or "").strip() or not kb_id:
            return []
        limit = max(1, min(int(limit or 3), 8))
        if scan_limit is None:
            scan_limit = max(
                50, int(_settings_value("SEMANTIC_STORE_LOG_SCAN_LIMIT", 2000) // 4)
            )

        q_tokens = lexical_tokens(question)
        q_sig = question_signature(question)
        if not q_tokens:
            return []
        hits: list[dict[str, Any]] = []

        # 1. Successful telemetry rows for THIS source, most recent first.
        try:
            from app.models.nl2sql_query_log import Nl2sqlQueryLog

            rows = (
                db.query(Nl2sqlQueryLog)
                .filter(
                    Nl2sqlQueryLog.datasource_id == kb_id,
                    Nl2sqlQueryLog.outcome == "success",
                    Nl2sqlQueryLog.generated_sql.isnot(None),
                )
                .order_by(Nl2sqlQueryLog.id.desc())
                .limit(int(scan_limit))
                .all()
            )
        except Exception as e:
            logger.debug("query_log_retrieval: log scan unavailable: %s", e)
            rows = []
        for row in rows:
            if question_signature(row.question or "") == q_sig:
                continue
            r_tokens = _row_tokens(row.question)
            if not r_tokens:
                continue
            coverage = len(q_tokens & r_tokens) / len(q_tokens)
            if coverage < _MIN_COVERAGE:
                continue
            hits.append(
                {
                    "question": row.question or "",
                    "sql": (row.generated_sql or "")[:_MAX_SQL_CHARS],
                    "source": "query_log",
                    "score": round(coverage, 3),
                    "outcome": "success",
                    "uses": 1,
                }
            )

        # 2. Verified plans for this KB — human-trusted shape, small boost.
        try:
            from app.models.query_plan_memory import QueryPlanMemory

            plans = (
                db.query(QueryPlanMemory)
                .filter(
                    QueryPlanMemory.kb_id == kb_id,
                    QueryPlanMemory.is_deleted.is_(False),
                )
                .limit(200)
                .all()
            )
        except Exception as e:
            logger.debug("query_log_retrieval: plan scan unavailable: %s", e)
            plans = []
        for plan in plans:
            if question_signature(plan.question or "") == q_sig:
                continue
            p_tokens = _row_tokens(plan.question)
            if not p_tokens:
                continue
            coverage = len(q_tokens & p_tokens) / len(q_tokens)
            if coverage < _MIN_COVERAGE:
                continue
            hits.append(
                {
                    "question": plan.question or "",
                    "sql": (plan.sql or "")[:_MAX_SQL_CHARS],
                    "source": "plan_memory",
                    # +0.1: a VERIFIED shape outranks an equally similar log row.
                    "score": round(coverage + 0.1, 3),
                    "outcome": "success",
                    "uses": int(plan.hits or 0),
                }
            )

        # Popularity only breaks ties — never creates a hit.
        hits.sort(key=lambda h: (-h["score"], -h["uses"], h["source"]))
        return hits[:limit]
    except Exception as e:
        logger.debug("query_log_retrieval: retrieval failed (fail-soft): %s", e)
        return []


def format_query_log_guidance(entries: list[dict[str, Any]]) -> str:
    """The few-shot prompt block. Empty string when there is nothing to say."""
    if not entries:
        return ""
    parts: list[str] = [
        "[Prior verified questions on this data source — reuse a matching "
        "shape (tables, joins, grouping); re-resolve any relative period "
        "against today:]"
    ]
    used = len(parts[0])
    for e in entries:
        line = f"- Q: {e.get('question', '')}\n  SQL shape: {e.get('sql', '')}"
        if used + len(line) > _MAX_TOTAL_CHARS:
            break
        parts.append(line)
        used += len(line) + 1
    return "\n".join(parts) if len(parts) > 1 else ""


def build_query_log_guidance(
    db: Session | None, kb_id: str, question: str, *, limit: int = 3
) -> str:
    """Flag-aware convenience wrapper for the SQL prompt builder."""
    if not _settings_value("NL2SQL_QUERY_LOG_RETRIEVAL_ENABLED", True):
        return ""
    try:
        return format_query_log_guidance(
            retrieve_similar_questions(db, kb_id, question, limit=limit)
        )
    except Exception as e:  # never break the served path over guidance
        logger.debug("query_log_retrieval: guidance build failed: %s", e)
        return ""
