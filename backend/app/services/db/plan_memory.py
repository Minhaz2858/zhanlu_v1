"""Load/save a verified question→plan mapping (see models/query_plan_memory.py).

Public surface:

* :func:`question_signature` — stable key for a question, so the same intent
  maps to the same plan across turns, agents and phrasing whitespace. Numbers
  are dropped ("top 10" ≡ "top 5") but period words are KEPT ("last month" ≠
  "last week"), because those are different questions.
* :func:`load_plan` / :func:`save_plan` — the store.
* :func:`format_plan_guidance` — the text injected into the SQL prompt. This is
  the whole point: the model keeps the same tables, columns and grouping it
  used last time, while re-resolving the period against today's date.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

_NUMBERS = re.compile(r"\b\d+\b")
_NON_WORD = re.compile(r"[^\w\u4e00-\u9fff]+")
_WS = re.compile(r"\s+")
# Guidance is a prompt injection, so it has to stay small: the SQL is a shape
# hint, not the answer.
_MAX_SQL_CHARS = 900


def question_signature(question: str) -> str:
    """Normalise a question into a stable, comparable key."""
    q = (question or "").lower()
    q = _NUMBERS.sub(" ", q)
    q = _NON_WORD.sub(" ", q)
    q = _WS.sub(" ", q).strip()
    return hashlib.sha256(q.encode("utf-8")).hexdigest()


def _looks_like_a_question(text: str) -> bool:
    return bool((text or "").strip())


def load_plan(db: Session, kb_id: str, question: str) -> Optional[dict[str, Any]]:
    """Return the verified plan for this (KB, question), or None.

    Never raises: a missing table (first boot before create_all) or any ORM
    error degrades to "no plan", which is the pre-existing behaviour.
    """
    if not _looks_like_a_question(question) or not kb_id:
        return None
    try:
        from app.models.query_plan_memory import QueryPlanMemory

        row = (
            db.query(QueryPlanMemory)
            .filter(
                QueryPlanMemory.kb_id == kb_id,
                QueryPlanMemory.signature == question_signature(question),
                QueryPlanMemory.is_deleted.is_(False),
            )
            .first()
        )
    except Exception:
        return None
    if row is None:
        return None
    return {
        "id": row.id,
        "question": row.question,
        "tables": list(row.tables or []),
        "sql": row.sql or "",
        "dimensions": list(row.dimensions or []),
        "hits": row.hits or 0,
    }


def save_plan(
    db: Session,
    kb_id: str,
    question: str,
    tables: list[str],
    sql: str,
    dimensions: list[str] | None = None,
) -> bool:
    """Record (or refresh) the plan that just produced a VERIFIED answer.

    Only call this when the answer passed the deterministic checks — a plan
    learned from an unverified answer would make the platform consistently
    wrong instead of occasionally wrong.
    """
    if not _looks_like_a_question(question) or not kb_id or not (sql or "").strip():
        return False
    try:
        from app.models.query_plan_memory import QueryPlanMemory

        sig = question_signature(question)
        row = (
            db.query(QueryPlanMemory)
            .filter(
                QueryPlanMemory.kb_id == kb_id,
                QueryPlanMemory.signature == sig,
                QueryPlanMemory.is_deleted.is_(False),
            )
            .first()
        )
        now = datetime.now(timezone.utc)
        if row is None:
            row = QueryPlanMemory(
                kb_id=kb_id,
                signature=sig,
                question=(question or "")[:2000],
                tables=list(tables or []),
                sql=sql,
                dimensions=list(dimensions or []),
                hits=0,
                verified_at=now,
            )
            db.add(row)
        else:
            row.question = (question or "")[:2000]
            row.tables = list(tables or [])
            row.sql = sql
            row.dimensions = list(dimensions or [])
            row.verified_at = now
        db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False


def mark_plan_used(db: Session, plan: dict[str, Any] | None) -> None:
    """Increment the reuse counter (observability: is the memory paying off?)."""
    if not plan or not plan.get("id"):
        return
    try:
        from app.models.query_plan_memory import QueryPlanMemory

        row = db.query(QueryPlanMemory).filter(QueryPlanMemory.id == plan["id"]).first()
        if row is not None:
            row.hits = (row.hits or 0) + 1
            db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def format_plan_guidance(plan: dict[str, Any] | None) -> str:
    """Prompt text that anchors a repeat question to its verified plan."""
    if not plan:
        return ""
    parts = [
        "This exact question has been answered before against THIS database. "
        "Answer it the same way:",
    ]
    tables = [t for t in (plan.get("tables") or []) if t]
    if tables:
        parts.append(f"- tables: {', '.join(str(t) for t in tables)}")
    dims = [d for d in (plan.get("dimensions") or []) if d]
    if dims:
        parts.append(f"- grouping: {', '.join(str(d) for d in dims)}")
    sql = _WS.sub(" ", (plan.get("sql") or "").strip())
    if sql:
        if len(sql) > _MAX_SQL_CHARS:
            sql = sql[:_MAX_SQL_CHARS] + " …"
        parts.append(f"- previously verified SQL: {sql}")
    parts.append(
        "Keep the same tables, columns and grouping. Re-resolve any relative "
        "period (e.g. \"last month\", \"this week\") against today's date — never "
        "copy a date literal from the previous SQL."
    )
    return "\n".join(parts)
