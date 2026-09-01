"""LLM answer quality evaluation pipeline.

Periodically samples conversations from the past 24h, runs the quality
evaluation (completeness, accuracy, helpfulness, safety) on the assistant
responses, and stores results.

Design:
1. **Sampling**: Random sample of conversations with rate ``EVAL_SAMPLE_RATE``
2. **Evaluation**: Uses ``quality_eval.evaluate_quality()`` with LLM-as-judge
3. **Storage**: Results stored in ``EvalResult`` table rows
4. **Reporting**: Daily summary report with pass rates per dimension

Configuration:
- ``EVAL_PIPELINE_ENABLED`` (default False)
- ``EVAL_SAMPLE_RATE`` (default 0.1 = 10% of conversations)
- ``EVAL_MAX_SAMPLES_PER_RUN`` (default 50) — cap per execution

Scheduled in ``scheduled_tasks.py`` as a daily task.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class EvalRecord:
    """A single evaluation record."""
    conversation_id: str = ""
    user_message: str = ""
    assistant_text: str = ""
    scores: dict = field(default_factory=dict)  # dimension → score
    verdict: str = "pending"
    evaluated_at: str = ""
    model: str = ""

    def to_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "user_message": self.user_message,
            "assistant_text": self.assistant_text[:500],
            "scores": self.scores,
            "verdict": self.verdict,
            "evaluated_at": self.evaluated_at,
            "model": self.model,
        }


def is_enabled() -> bool:
    from app.config import settings
    return getattr(settings, "EVAL_PIPELINE_ENABLED", False)


def sample_rate() -> float:
    from app.config import settings
    return getattr(settings, "EVAL_SAMPLE_RATE", 0.1)


def max_samples() -> int:
    from app.config import settings
    return getattr(settings, "EVAL_MAX_SAMPLES_PER_RUN", 50)


def _sample_conversations(
    db,
    since: Optional[datetime] = None,
) -> list[dict]:
    """Sample conversations from the database.

    Returns a list of dicts with conversation_id, user_message, assistant_text.

    NOTE (2026-08-29, Phase 0 build): the original code imported a
    ``Conversation`` model from ``app.models.chat`` that does not exist in
    this checkout — a dormant bug (the pipeline was flag-gated so the import
    was never exercised). The real model is ``AgentConversation``
    (``app.models.agent_conversation``) whose ``messages`` JSON column holds
    ``[{role, content}, ...]``.
    """
    from app.models.agent_conversation import AgentConversation

    cutoff = since or (datetime.now(timezone.utc) - timedelta(hours=24))

    try:
        conversations = (
            db.query(AgentConversation)
            .filter(
                AgentConversation.updated_date >= cutoff,
                AgentConversation.is_deleted == False,  # noqa: E712
            )
            .order_by(AgentConversation.updated_date.desc())
            .limit(max_samples() * 3)  # oversample for filtering
            .all()
        )
    except Exception as e:
        logger.warning("Eval pipeline: failed to query conversations: %s", e)
        return []

    sampled: list[dict] = []
    for conv in conversations:
        if len(sampled) >= max_samples():
            break
        if random.random() > sample_rate():
            continue

        messages = conv.messages or []
        if not isinstance(messages, list) or len(messages) < 2:
            continue

        # Find the last user-assistant pair
        user_msg = None
        assistant_msg = None
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if not isinstance(m, dict):
                continue
            if m.get("role") == "assistant" and assistant_msg is None:
                assistant_msg = m
            if m.get("role") == "user" and user_msg is None:
                user_msg = m

        if user_msg and assistant_msg:
            sampled.append({
                "conversation_id": conv.id,
                "user_message": user_msg.get("content", ""),
                "assistant_text": assistant_msg.get("content", ""),
            })

    return sampled


def run_eval_pipeline(db) -> list[EvalRecord]:
    """Run the full evaluation pipeline.

    Args:
        db: SQLAlchemy Session (sync).

    Returns:
        List of EvalRecord results.
    """
    if not is_enabled():
        logger.debug("Eval pipeline disabled — skipping")
        return []

    conversations = _sample_conversations(db)
    if not conversations:
        logger.info("Eval pipeline: no conversations to evaluate")
        return []

    results: list[EvalRecord] = []
    for conv in conversations:
        try:
            record = _evaluate_one(conv)
            if record:
                results.append(record)
                _persist_result(db, record)
        except Exception as e:
            logger.debug("Eval pipeline: failed for conv %s: %s", conv["conversation_id"], e)

    logger.info("Eval pipeline: evaluated %d/%d conversations", len(results), len(conversations))
    return results


def _evaluate_one(conv: dict) -> Optional[EvalRecord]:
    """Evaluate a single conversation pair."""
    from app.services.synexia.quality_eval import evaluate_quality

    result = evaluate_quality(
        user_message=conv["user_message"],
        assistant_text=conv["assistant_text"],
        task_spec={"source": "eval_pipeline"},
    )

    # QualityEvalResult exposes completeness_score + confidence (no `scores`
    # dict / `model` — the original mapping was written against a different
    # API and crashed as soon as the flag was enabled; fixed 2026-08-29).
    return EvalRecord(
        conversation_id=conv["conversation_id"],
        user_message=conv["user_message"],
        assistant_text=conv["assistant_text"],
        scores={
            "completeness": getattr(result, "completeness_score", 0.0) or 0.0,
            "confidence": getattr(result, "confidence", 0.0) or 0.0,
        },
        verdict=result.verdict or "accept",
        evaluated_at=datetime.now(timezone.utc).isoformat(),
        model="",
    )


def _persist_result(db, record: EvalRecord) -> None:
    """Persist evaluation result to database."""
    try:
        from app.models.eval_result import EvalResult
        entry = EvalResult(
            conversation_id=record.conversation_id,
            user_message=record.user_message,
            assistant_text=record.assistant_text,
            scores=json.dumps(record.scores),
            verdict=record.verdict,
            model=record.model,
        )
        db.add(entry)
        db.commit()
    except Exception as e:
        logger.warning("Eval pipeline: failed to persist result: %s", e)
        db.rollback()


def build_daily_report(results: list[EvalRecord]) -> dict:
    """Build a summary report from evaluation results."""
    if not results:
        return {"total": 0, "pass_rate": 0.0, "dimensions": {}}

    accept_count = sum(1 for r in results if r.verdict == "accept")
    dim_scores: dict[str, list[float]] = {}
    for r in results:
        for dim, score in r.scores.items():
            dim_scores.setdefault(dim, []).append(score)

    dim_avg = {dim: sum(s) / max(len(s), 1) for dim, s in dim_scores.items()}

    return {
        "total": len(results),
        "pass_rate": round(accept_count / max(len(results), 1), 3),
        "dimensions": dim_avg,
    }


__all__ = [
    "EvalRecord",
    "is_enabled",
    "run_eval_pipeline",
    "build_daily_report",
]
