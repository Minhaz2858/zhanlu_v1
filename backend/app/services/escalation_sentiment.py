"""P3-6b: sentiment auto-escalation signal — embedding-based, fails open.

Detects frustration/negative sentiment in the inbound user message and,
when strong, appends a short nudge to the system prompt so the agent
acknowledges the emotion and offers/uses ``escalate_to_human`` instead
of plowing on. This is the IBM "sentiment-based escalation" best
practice — the agent may still miss it, so this is a second net.

Honesty/safety:
- local embeddings only (EMBEDDING_PROVIDER=local) — no API cost, no
  user text leaves the box for this check
- threshold is conservative; the block is ADVISORY (never blocks chat,
  never force-creates escalations on its own)
- any failure → no block (fail open), logged once per burst
"""

from __future__ import annotations

import functools
import logging
import math

logger = logging.getLogger(__name__)

# Paraphrased frustration / human-request exemplars (EN + zh).
_FRUSTRATION_EXEMPLARS = [
    "this is the third time I'm asking and nobody listens",
    "your product is broken and your support is useless",
    "I want to speak to a real human right now",
    "I am extremely frustrated with this terrible service",
    "stop giving me the runaround and just fix it",
    "this is unacceptable, I want a refund and a manager",
    "why is this so complicated, I'm losing my patience",
    "你们的服务太差了，我要投诉",
    "我要找人工客服，不要再让我跟机器人说话",
    "这个问题你们一直解决不了，我非常不满意",
]

# Cosine-similarity threshold tuned conservatively: only strong matches
# fire (a mildly annoyed "hmm, that's odd" must NOT trigger).
_THRESHOLD = 0.55
_MAX_TEXT = 1000


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


@functools.lru_cache(maxsize=8)
def _exemplar_embeddings() -> list[list[float]] | None:
    try:
        from app.services.memory_advanced.embeddings import (
            get_embeddings_batch,
        )

        vecs = get_embeddings_batch(_FRUSTRATION_EXEMPLARS)
        return vecs if vecs and all(v for v in vecs) else None
    except Exception as exc:  # noqa: BLE001 — fail open
        logger.warning("sentiment: exemplar embeddings unavailable: %s", exc)
        return None


def negative_sentiment_score(text: str | None) -> float:
    """Max cosine similarity against frustration exemplars (0..1).
    Returns 0.0 on any failure — sentiment must never break chat."""
    if not text or not text.strip():
        return 0.0
    try:
        exemplars = _exemplar_embeddings()
        if not exemplars:
            return 0.0
        from app.services.memory_advanced.embeddings import get_embedding

        result = get_embedding(text[:_MAX_TEXT])
        if not result:
            return 0.0
        # get_embedding returns an EmbeddingResult (.vector) or bare list
        vec = getattr(result, "vector", None) or (
            result if isinstance(result, list) else None)
        if not vec:
            return 0.0
        return max(_cosine(vec, e) for e in exemplars)
    except Exception as exc:  # noqa: BLE001 — fail open
        logger.warning("sentiment: scoring failed (fail-open): %s", exc)
        return 0.0


def escalation_nudge_for_message(text: str | None) -> str:
    """Advisory system-prompt block when frustration is strong."""
    try:
        score = negative_sentiment_score(text)
        if score < _THRESHOLD:
            return ""
        return (
            "\n\n[USER FRUSTRATION SIGNAL — handle with care]\n"
            "The user's message strongly signals frustration or a wish "
            "for a human (confidence %.2f). Acknowledge it plainly, do "
            "NOT be defensive, keep the reply short and concrete. If you "
            "cannot resolve the issue with your tools in this turn, use "
            "the `escalate_to_human` tool with a full-context handoff "
            "instead of repeating yourself." % score
        )
    except Exception:  # noqa: BLE001 — fail open
        return ""
