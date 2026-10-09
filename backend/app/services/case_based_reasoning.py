"""Case-based reasoning over episodic memory (IBM agent-memory, P1-3).

CoALA-style memory taxonomy applied to Zhanlu's AgentMemory:

- ``semantic``  — facts/notes (existing behavior, default for legacy rows)
- ``episodic``  — task→solution cases recorded after successful turns;
  recalled for similar future tasks (case-based reasoning)
- ``procedural`` — promoted playbooks (P1-5); recalled like cases but
  always injected when relevant

Fail-safe everywhere: recording/recall never break the chat loop.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.agent_memory import AgentMemory

logger = logging.getLogger(__name__)

CASE_TYPES = ("episodic", "procedural")


def _case_content(task: str, solution: str, outcome: str) -> str:
    return (
        f"[CASE] TASK: {(task or '').strip()[:600]}\n"
        f"SOLUTION: {(solution or '').strip()[:1500]}\n"
        f"OUTCOME: {outcome}"
    )


def record_case(
    db: Session,
    agent_app_id: str,
    task: str,
    solution: str,
    *,
    outcome: str = "success",
    project_id: Optional[str] = None,
    memory_type: str = "episodic",
    importance: int = 2,
) -> Optional[str]:
    """Persist one task→solution case as an episodic memory row.

    Deduplicated by content hash; embedding attached best-effort for
    semantic recall. Returns the row id or None.
    """
    try:
        from app.config import settings

        if not getattr(settings, "CBR_RECORDING_ENABLED", True):
            return None
        task = (task or "").strip()
        solution = (solution or "").strip()
        if len(task) < 10 or len(solution) < 40:
            return None  # not substantive enough to learn from

        from app.services.memory_advanced import (
            compute_content_hash,
            is_duplicate_content,
        )

        content = _case_content(task, solution, outcome)
        if is_duplicate_content(db, agent_app_id, content):
            return None

        embedding = None
        try:
            from app.services.memory_advanced.embeddings import get_embedding

            emb = get_embedding(f"{task}\n{solution[:400]}")
            if emb and emb.vector:
                embedding = emb.vector
        except Exception as _emb_err:  # noqa: BLE001 — best-effort
            logger.debug("cbr: case embedding skipped: %s", _emb_err)

        row = AgentMemory(
            agent_app_id=agent_app_id,
            target="memory",
            content=content,
            char_count=len(content),
            content_hash=compute_content_hash(content),
            importance=importance,
            project_id=project_id or None,
            memory_type=memory_type if memory_type in CASE_TYPES else "episodic",
            embedding=embedding,
        )
        db.add(row)
        db.commit()
        logger.info("cbr: recorded %s case for agent %s", memory_type, agent_app_id)
        return row.id
    except Exception as exc:  # noqa: BLE001 — never break the chat loop
        logger.warning("cbr: record_case failed (non-fatal): %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def _lexical_score(query: str, text: str) -> float:
    import re

    q = {t for t in re.findall(r"\w+", (query or "").lower()) if len(t) > 2}
    t = {t for t in re.findall(r"\w+", (text or "").lower()) if len(t) > 2}
    if not q or not t:
        return 0.0
    return len(q & t) / (len(q) ** 0.5 * len(t) ** 0.5)


def _cosine(a: list[float], b: list[float]) -> float:
    import math

    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sum(x * y for x, y in zip(a[:n], b[:n]))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def find_similar_cases(
    db: Session,
    agent_app_id: str,
    task: str,
    *,
    k: int = 3,
    project_id: Optional[str] = None,
    min_similarity: float = 0.15,
) -> list[dict]:
    """Top-k episodic/procedural cases similar to ``task`` (best-effort).

    Ranks by stored-embedding cosine when available, lexical overlap
    otherwise. Project-scoped: matches the active project or unscoped
    legacy rows.
    """
    try:
        rows = (
            db.query(AgentMemory)
            .filter(
                AgentMemory.agent_app_id == agent_app_id,
                AgentMemory.target == "memory",
                AgentMemory.memory_type.in_(CASE_TYPES),
                AgentMemory.is_deleted == False,  # noqa: E712
            )
            .order_by(AgentMemory.created_date.desc())
            .limit(200)
            .all()
        )
        rows = [
            r for r in rows
            if not project_id or r.project_id in (None, project_id)
        ]
        if not rows:
            return []

        task_emb = None
        try:
            from app.services.memory_advanced.embeddings import get_embedding

            emb = get_embedding(task)
            if emb and emb.vector:
                task_emb = emb.vector
        except Exception:  # noqa: BLE001
            task_emb = None

        scored = []
        for r in rows:
            if task_emb is not None and r.embedding:
                score = _cosine(task_emb, r.embedding)
            else:
                score = _lexical_score(task, r.content)
            if score >= min_similarity:
                scored.append((score, r))
        scored.sort(key=lambda pair: -pair[0])
        return [
            {"score": round(s, 4), "memory": r} for s, r in scored[:k]
        ]
    except Exception as exc:  # noqa: BLE001 — advisory recall only
        logger.debug("cbr: find_similar_cases failed: %s", exc)
        return []


def build_cbr_block(
    db: Session,
    agent_app_id: str,
    task: str,
    *,
    project_id: Optional[str] = None,
) -> str:
    """Formatted [RELEVANT PAST EXPERIENCE] prompt block, or ''. """
    try:
        from app.config import settings

        if not getattr(settings, "CBR_RECALL_ENABLED", True):
            return ""
        task = (task or "").strip()
        if len(task) < 10:
            return ""
        cases = find_similar_cases(
            db, agent_app_id, task, k=3, project_id=project_id)
        if not cases:
            return ""
        lines = [
            "[RELEVANT PAST EXPERIENCE] How similar earlier tasks were "
            "solved — reuse what worked, avoid repeating failures:"
        ]
        for c in cases:
            content = (c["memory"].content or "").strip()
            task_line, solution_line = "", ""
            for ln in content.splitlines():
                if ln.startswith("[CASE] TASK:"):
                    task_line = ln[len("[CASE] TASK:"):].strip()
                elif ln.startswith("SOLUTION:"):
                    solution_line = ln[len("SOLUTION:"):].strip()
            if task_line and solution_line:
                lines.append(
                    f"- TASK: {task_line[:200]}\n"
                    f"  SOLUTION: {solution_line[:400]}"
                )
        if len(lines) <= 1:
            return ""
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 — advisory block only
        logger.debug("cbr: build_cbr_block failed: %s", exc)
        return ""


__all__ = [
    "record_case",
    "find_similar_cases",
    "build_cbr_block",
    "CASE_TYPES",
]
