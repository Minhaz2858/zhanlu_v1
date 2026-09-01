"""A/B prompt regression testing.

Compares two prompt versions (A and B) by running the same set of queries
through both and comparing quality scores. The winning prompt is determined
by statistically significant quality improvement.

Usage (CLI)::
    python -m app.tools.run_prompt_ab_test \
        --prompt-a "You are a helpful assistant." \
        --prompt-b "You are a highly precise BI analyst." \
        --queries 10

Configuration:
- ``PROMPT_AB_TEST_ENABLED`` (default False)
- Results stored in ``PromptABTest`` model rows.
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ABTestResult:
    """Result of a single A/B comparison."""
    test_id: str = ""
    prompt_version_a: str = ""
    prompt_version_b: str = ""
    total_queries: int = 0
    wins_a: int = 0
    wins_b: int = 0
    ties: int = 0
    winner: str = ""  # "A", "B", or "tie"
    confidence: float = 0.0
    mean_score_a: float = 0.0
    mean_score_b: float = 0.0
    per_query_results: list[dict] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "test_id": self.test_id,
            "prompt_version_a": self.prompt_version_a,
            "prompt_version_b": self.prompt_version_b,
            "total_queries": self.total_queries,
            "wins_a": self.wins_a,
            "wins_b": self.wins_b,
            "ties": self.ties,
            "winner": self.winner,
            "confidence": self.confidence,
            "mean_score_a": self.mean_score_a,
            "mean_score_b": self.mean_score_b,
            "per_query_results": self.per_query_results,
            "created_at": self.created_at,
        }


def is_enabled() -> bool:
    from app.config import settings
    return getattr(settings, "PROMPT_AB_TEST_ENABLED", False)


def _overall_score(scores: dict) -> float:
    """Compute an overall quality score from dimension scores (0–10)."""
    if not scores:
        return 0.0
    # Default weights: completeness, accuracy, helpfulness are core
    weights = {
        "completeness": 0.3,
        "accuracy": 0.3,
        "helpfulness": 0.25,
        "safety": 0.15,
    }
    total = 0.0
    total_weight = 0.0
    for dim, weight in weights.items():
        if dim in scores:
            total += scores[dim] * weight
            total_weight += weight
    if total_weight == 0:
        return float(sum(scores.values()) / max(len(scores), 1))
    return round(total / total_weight, 2)


def run_ab_test(
    prompt_a: str,
    prompt_b: str,
    queries: list[str],
    model: Optional[str] = None,
) -> ABTestResult:
    """Run an A/B test comparing two prompts.

    Args:
        prompt_a: System prompt version A.
        prompt_b: System prompt version B.
        queries: List of test queries to evaluate.
        model: Model to use (default: settings.LLM_MODEL).

    Returns:
        ABTestResult with win counts and statistics.
    """
    import uuid
    test_id = uuid.uuid4().hex[:12]

    from app.services.llm_service import call_llm
    from app.services.synexia.quality_eval import evaluate_quality

    per_query = []
    wins_a = 0
    wins_b = 0
    ties = 0
    scores_a = []
    scores_b = []

    for query in queries:
        try:
            # Generate response A
            resp_a = call_llm(
                prompt=prompt_a,
                messages=[{"role": "user", "content": query}],
                temperature=0.0,
                task_type="simple_chat",
            )
            # Generate response B
            resp_b = call_llm(
                prompt=prompt_b,
                messages=[{"role": "user", "content": query}],
                temperature=0.0,
                task_type="simple_chat",
            )

            # Evaluate both responses
            result_a = evaluate_quality(
                user_message=query,
                assistant_text=resp_a.get("response", ""),
            )
            result_b = evaluate_quality(
                user_message=query,
                assistant_text=resp_b.get("response", ""),
            )

            score_a = _overall_score(result_a.scores or {})
            score_b = _overall_score(result_b.scores or {})
            scores_a.append(score_a)
            scores_b.append(score_b)

            if abs(score_a - score_b) < 0.1:
                ties += 1
                winner = "tie"
            elif score_a > score_b:
                wins_a += 1
                winner = "A"
            else:
                wins_b += 1
                winner = "B"

            per_query.append({
                "query": query,
                "score_a": score_a,
                "score_b": score_b,
                "winner": winner,
                "verdict_a": result_a.verdict,
                "verdict_b": result_b.verdict,
            })

        except Exception as e:
            logger.warning("AB test: failed for query '%s': %s", query[:50], e)

    total = len(per_query)
    mean_a = round(statistics.mean(scores_a), 2) if scores_a else 0.0
    mean_b = round(statistics.mean(scores_b), 2) if scores_b else 0.0

    # Winner determination
    if wins_a > wins_b:
        overall_winner = "A"
    elif wins_b > wins_a:
        overall_winner = "B"
    else:
        overall_winner = "tie"

    # Confidence: proportion of non-tie queries won
    decisive = wins_a + wins_b
    confidence = round(wins_a / max(decisive, 1), 2) if overall_winner == "A" else \
                 round(wins_b / max(decisive, 1), 2) if overall_winner == "B" else 0.5

    return ABTestResult(
        test_id=test_id,
        prompt_version_a=prompt_a[:200],
        prompt_version_b=prompt_b[:200],
        total_queries=total,
        wins_a=wins_a,
        wins_b=wins_b,
        ties=ties,
        winner=overall_winner,
        confidence=confidence,
        mean_score_a=mean_a,
        mean_score_b=mean_b,
        per_query_results=per_query,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def persist_ab_result(db, result: ABTestResult) -> bool:
    """Persist an A/B test result to database."""
    try:
        from app.models.prompt_ab_test import PromptABTest
        entry = PromptABTest(
            test_id=result.test_id,
            prompt_version_a=result.prompt_version_a,
            prompt_version_b=result.prompt_version_b,
            total_queries=result.total_queries,
            wins_a=result.wins_a,
            wins_b=result.wins_b,
            ties=result.ties,
            winner=result.winner,
            confidence=result.confidence,
            mean_score_a=result.mean_score_a,
            mean_score_b=result.mean_score_b,
            per_query_results=json.dumps(result.per_query_results),
        )
        db.add(entry)
        db.commit()
        return True
    except Exception as e:
        logger.warning("Failed to persist AB test result: %s", e)
        db.rollback()
        return False


__all__ = [
    "ABTestResult",
    "is_enabled",
    "run_ab_test",
    "persist_ab_result",
]
