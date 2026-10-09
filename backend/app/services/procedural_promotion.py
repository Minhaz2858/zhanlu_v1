"""Procedural memory promotion (IBM agent-memory, P1-5).

When several episodic task→solution cases describe the SAME kind of task,
that repetition is a reusable skill. This job clusters recent episodic
cases (greedy lexical-similarity clustering), distills each sufficiently
large cluster into ONE procedural playbook memory via a cheap LLM call
(template fallback when the LLM is unavailable), and soft-deletes the
consolidated episodic rows. Procedural memories are recalled by
``case_based_reasoning.find_similar_cases`` like cases, but represent
"how this agent does this kind of task" — the CoALA procedural tier.

Flag: ``PROCEDURAL_PROMOTION_ENABLED`` (scheduled every 24h via
scheduled_tasks, mirroring loop_postmortem).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_PROMPT = (
    "You are distilling an agent's repeated successes into a reusable "
    "playbook. Below are N episodic cases (task + how it was solved). "
    "Write ONE compact playbook for future runs of this task pattern:\n"
    "TASK-PATTERN: <one line describing the recurring task>\n"
    "STEPS: <3-6 imperative steps, semicolons separated>\n"
    "Return ONLY the two lines, no preamble.\n\n"
)


def _recent_cases(db, limit: int = 100) -> list:
    from app.models.agent_memory import AgentMemory

    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    rows = (
        db.query(AgentMemory)
        .filter(
            AgentMemory.memory_type == "episodic",
            AgentMemory.is_deleted == False,  # noqa: E712
            AgentMemory.created_date >= cutoff,
        )
        .order_by(AgentMemory.created_date.desc())
        .limit(limit)
        .all()
    )
    return [r for r in rows if "[CASE] TASK:" in (r.content or "")]


def _pair_score(a: str, b: str) -> float:
    from app.services.case_based_reasoning import _lexical_score

    return _lexical_score(a, b)


def _cluster(cases: list, threshold: float = 0.55) -> list[list]:
    """Greedy clustering by pairwise lexical similarity of task lines."""
    def task_of(content: str) -> str:
        for ln in content.splitlines():
            if ln.startswith("[CASE] TASK:"):
                return ln[len("[CASE] TASK:"):].strip().lower()
        return content[:120].lower()

    clusters: list[list] = []
    tasks = [task_of(c.content or "") for c in cases]
    for i, row in enumerate(cases):
        placed = False
        for cluster in clusters:
            rep = tasks[cluster[0]]
            if _pair_score(tasks[i], rep) >= threshold:
                cluster.append(i)
                placed = True
                break
        if not placed:
            clusters.append([i])
    return clusters


def _hash_key(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()


def _parse_playbook(raw: str) -> str:
    start = raw.find("TASK-PATTERN")
    if start == -1:
        return ""
    lines = [ln.strip() for ln in raw[start:].splitlines() if ln.strip()]
    task_line = next((l for l in lines if l.startswith("TASK-PATTERN")), "")
    steps_line = next((l for l in lines if l.startswith("STEPS")), "")
    if not task_line or not steps_line:
        return ""
    return f"[PLAYBOOK] {task_line}\n{steps_line}"


def _template_playbook(cluster_rows: list) -> str:
    tasks, solutions = [], []
    for r in cluster_rows:
        for ln in (r.content or "").splitlines():
            if ln.startswith("[CASE] TASK:") and not tasks:
                tasks.append(ln[len("[CASE] TASK:"):].strip())
            elif ln.startswith("SOLUTION:") and len(solutions) < 3:
                solutions.append(ln[len("SOLUTION:"):].strip())
    return (
        f"[PLAYBOOK] TASK-PATTERN: {tasks[0][:200] if tasks else 'recurring task'}\n"
        f"STEPS: {'; '.join(s[:200] for s in solutions) or 'see member cases'}"
    )


async def run_procedural_promotion(db, max_playbooks: int = 3) -> dict:
    """Cluster recent episodic cases → procedural playbooks. Best-effort."""
    from app.config import settings

    if not getattr(settings, "PROCEDURAL_PROMOTION_ENABLED", True):
        return {"enabled": False, "playbooks": 0}

    cases = _recent_cases(db)
    if len(cases) < 3:
        return {"enabled": True, "playbooks": 0, "cases": len(cases)}

    from app.models.agent_memory import AgentMemory

    clusters = [
        c for c in _cluster(cases) if len(c) >= 3
    ]
    if not clusters:
        return {"enabled": True, "playbooks": 0, "cases": len(cases)}

    from app.services.llm_service import call_llm

    created = 0
    for cluster in clusters[:max_playbooks]:
        member_rows = [cases[i] for i in cluster]
        members_text = "\n\n".join(
            r.content[:900] for r in member_rows[:5]
        )
        playbook = ""
        try:
            res = await call_llm(
                prompt=_PROMPT + members_text,
                messages=[],
                temperature=0.2,
                task_type="simple_chat",
            )
            raw = res.get("response") or res.get("content") or ""
            playbook = _parse_playbook(raw)
        except Exception as exc:  # noqa: BLE001 — template fallback
            logger.debug("procedural_promotion: LLM distill failed: %s", exc)
        if not playbook:
            playbook = _template_playbook(member_rows)

        key = _hash_key(playbook)
        exists = (
            db.query(AgentMemory)
            .filter(
                AgentMemory.content_hash == key,
                AgentMemory.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if exists is not None:
            continue

        agent_app_id = member_rows[0].agent_app_id
        project_id = member_rows[0].project_id
        db.add(AgentMemory(
            agent_app_id=agent_app_id,
            target="memory",
            content=playbook[:4000],
            char_count=len(playbook),
            content_hash=key,
            importance=3,
            project_id=project_id,
            memory_type="procedural",
        ))
        # Consolidated: retire the member episodic rows (audit trail in logs).
        for member in member_rows:
            member.is_deleted = True
        created += 1
        logger.info(
            "procedural_promotion: %d cases → 1 playbook for agent %s",
            len(member_rows), agent_app_id,
        )
    db.commit()
    return {"enabled": True, "playbooks": created}


def _run_sync() -> dict:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        import asyncio

        return asyncio.run(run_procedural_promotion(db))
    finally:
        db.close()


async def promotion_cycle() -> None:
    """Scheduled wrapper — no-op unless PROCEDURAL_PROMOTION_ENABLED."""
    from app.config import settings

    if not getattr(settings, "PROCEDURAL_PROMOTION_ENABLED", True):
        return
    try:
        import asyncio

        summary = await asyncio.to_thread(_run_sync)
        if summary.get("playbooks"):
            logger.info("[procedural_promotion] %s", summary)
    except Exception:
        logger.exception("[procedural_promotion] Cycle failed")


__all__ = ["run_procedural_promotion", "promotion_cycle"]
