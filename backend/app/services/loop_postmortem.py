"""Loop post-mortem — mine failed ReAct loops into reusable lessons (gap #4).

IBM ReAct: "augmented with external memory, agents learn from past mistakes."
The platform already mines SUCCESSFUL SQL (q_sql_examples); this closes the
failure side: it samples recent failed delegations/turns from
``agent_invocations``, distills a one-line lesson per cluster via a cheap
LLM call, and stores it as an ``AgentMemory`` row (target="memory") so the
dynamic prompt builder surfaces it in future turns.

Flag: ``LOOP_POSTMORTEM_ENABLED`` (scheduled every 12h via scheduled_tasks).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def _recent_failures(db, limit: int = 10) -> list[dict]:
    """Recent failed invocations (sub-agents + main loop) from the last 24h."""
    from app.models.agent_invocation import AgentInvocation

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = (
        db.query(AgentInvocation)
        .filter(
            AgentInvocation.status == "failed",
            AgentInvocation.created_date >= cutoff,
        )
        .order_by(AgentInvocation.created_date.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "task": (r.input_message or "")[:300],
            "error": (r.error_message or "")[:300],
            "route": r.route or "",
            "invocation_id": r.id,
        }
        for r in rows
        if (r.error_message or "").strip()
    ]


def _lesson_key(lesson: str) -> str:
    return hashlib.sha256(lesson.strip().lower().encode()).hexdigest()


async def run_loop_postmortem(db, max_failures: int = 8) -> dict:
    """Distill recent failures into AgentMemory lessons. Best-effort."""
    from app.config import settings

    if not getattr(settings, "LOOP_POSTMORTEM_ENABLED", False):
        return {"enabled": False, "lessons": 0}

    failures = _recent_failures(db, limit=max_failures)
    if not failures:
        return {"enabled": True, "lessons": 0}

    from app.services.llm_service import call_llm

    failures_text = "\n".join(
        f"- task: {f['task']!r} | route={f['route'] or 'n/a'} | error: {f['error']}"
        for f in failures
    )
    try:
        res = await call_llm(
            prompt=(
                "You are an agent-reliability analyst. Below are recent FAILED "
                "agent loop invocations. Distill the SHARED root causes into "
                "AT MOST 3 short, actionable lessons for future agent runs "
                "(one sentence each, imperative, e.g. 'Verify column names "
                "with describe_schema before querying erp_t_stk_inventory — "
                "its date columns are frequently NULL'). "
                "Return ONLY a JSON array of strings.\n\n" + failures_text
            ),
            messages=[],
            temperature=0.2,
            task_type="simple_chat",
        )
        raw = res.get("response") or res.get("content") or ""
    except Exception as e:  # noqa: BLE001 — post-mortem must never crash
        logger.warning("loop_postmortem: LLM distillation failed: %s", e)
        return {"enabled": True, "lessons": 0, "error": str(e)}

    import json as _json

    try:
        start, end = raw.find("["), raw.rfind("]")
        lessons = _json.loads(raw[start : end + 1]) if start != -1 and end > start else []
        lessons = [str(x).strip() for x in lessons if str(x).strip()][:3]
    except (ValueError, TypeError):
        lessons = []

    from app.models.agent_memory import AgentMemory

    inserted = 0
    default_agent = _resolve_default_agent_id(db)
    for lesson in lessons:
        key = _lesson_key(lesson)
        exists = (
            db.query(AgentMemory)
            .filter(AgentMemory.content_hash == key,
                    AgentMemory.is_deleted == False)  # noqa: E712
            .first()
        )
        if exists is not None:
            continue
        content = f"LESSON (from failed agent loops): {lesson}"
        db.add(AgentMemory(
            agent_app_id=default_agent or "system",
            target="memory",
            content=content[:4000],
            char_count=len(content),
            content_hash=key,
            importance=2,
        ))
        inserted += 1
    db.commit()
    logger.info("loop_postmortem: %d failures → %d new lessons", len(failures), inserted)
    return {"enabled": True, "failures": len(failures), "lessons": inserted}


def _resolve_default_agent_id(db) -> str | None:
    """Bind lessons to the general_assistant app so memory injection finds them."""
    try:
        from app.models.agent_app import AgentApp

        app = (
            db.query(AgentApp)
            .filter(AgentApp.name == "general_assistant",
                    AgentApp.is_deleted == False)  # noqa: E712
            .first()
        )
        return app.id if app else None
    except Exception:  # noqa: BLE001
        return None


__all__ = ["run_loop_postmortem"]
