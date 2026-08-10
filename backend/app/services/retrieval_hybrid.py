"""Semantic retrieval hybrid for long conversation history.

When conversation history approaches the model's context window, instead of
immediately triggering a full compaction (which is expensive and may lose
detail), this module first attempts to retrieve the most relevant messages
from history using semantic similarity.

Workflow:
1. Before compaction: search conversation history for messages semantically
   similar to the current user query.
2. If enough relevant messages are found (covering the query's topic), skip
   full compaction — just inject those relevant messages as context.
3. If not enough relevant messages found, fall back to normal compaction.

Uses the existing ``get_embedding()`` to embed the user query and cosine
similarity against stored message embeddings.

Configuration:
- ``RETRIEVAL_HYBRID_ENABLED`` (default False)
- ``RETRIEVAL_HYBRID_TOP_K`` (default 10) — max messages to retrieve
- ``RETRIEVAL_HYBRID_MIN_SCORE`` (default 0.5) — min cosine similarity
"""

from __future__ import annotations

import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two embedding vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def is_enabled() -> bool:
    from app.config import settings
    return getattr(settings, "RETRIEVAL_HYBRID_ENABLED", False)


def top_k() -> int:
    from app.config import settings
    return getattr(settings, "RETRIEVAL_HYBRID_TOP_K", 10)


def min_score() -> float:
    from app.config import settings
    return getattr(settings, "RETRIEVAL_HYBRID_MIN_SCORE", 0.5)


def retrieve_relevant_messages(
    user_query: str,
    history: list[dict],
    top_k: Optional[int] = None,
    min_similarity: Optional[float] = None,
) -> list[dict]:
    """Retrieve top-N messages from history most relevant to the user query.

    Messages must have an ``embedding`` field (list[float]) to participate.
    The user query is embedded on-the-fly via ``get_embedding()``.

    Args:
        user_query: The current user message text.
        history: List of message dicts, each MAY have an ``embedding`` field.
        top_k: Max messages to return (default: RETRIEVAL_HYBRID_TOP_K).
        min_similarity: Minimum cosine score (default: RETRIEVAL_HYBRID_MIN_SCORE).

    Returns:
        Sorted list of messages (highest similarity first), each with an
        added ``_retrieval_score`` field.
    """
    if not is_enabled():
        return []

    k = top_k
    if k is None:
        from app.config import settings
        k = getattr(settings, "RETRIEVAL_HYBRID_TOP_K", 10)
    threshold = min_similarity or min_score()

    if not user_query or not history:
        return []

    # Embed the user query
    try:
        from app.services.llm_service import get_embedding
        query_embedding = get_embedding(user_query)
    except Exception as e:
        logger.warning("Failed to embed query for retrieval hybrid: %s", e)
        return []

    # Score all messages with embeddings
    scored: list[tuple[float, dict]] = []
    for msg in history:
        emb = msg.get("embedding")
        if emb is None or not isinstance(emb, list):
            continue
        score = _cosine_similarity(query_embedding, emb)
        if score >= threshold:
            scored.append((score, msg))

    # Sort by score descending, take top-k
    scored.sort(key=lambda x: x[0], reverse=True)
    results = scored[:k]

    # Annotate with retrieval score
    annotated = []
    for score, msg in results:
        msg_copy = dict(msg)
        msg_copy["_retrieval_score"] = round(score, 4)
        annotated.append(msg_copy)

    if annotated:
        logger.debug(
            "Retrieval hybrid: found %d relevant messages for query (top_score=%.3f)",
            len(annotated), annotated[0]["_retrieval_score"] if annotated else 0.0,
        )

    return annotated


def should_skip_compaction(
    user_query: str,
    history: list[dict],
    min_relevant: int = 3,
) -> bool:
    """Return True if semantic retrieval found enough context to skip compaction.

    Args:
        user_query: Current user message.
        history: Conversation history with embeddings.
        min_relevant: Minimum number of relevant messages to skip compaction.

    Returns:
        True if compaction can be skipped (enough relevant messages found).
    """
    relevant = retrieve_relevant_messages(user_query, history)
    return len(relevant) >= min_relevant


def build_retrieval_context_block(
    user_query: str,
    history: list[dict],
    top_k: Optional[int] = None,
) -> str:
    """Build a context block from retrieved messages for injection into the prompt.

    Returns an empty string if retrieval hybrid is disabled or no messages found.
    """
    messages = retrieve_relevant_messages(user_query, history, top_k=top_k)
    if not messages:
        return ""

    lines = ["[Conversation History — Retrieved Relevant Messages]"]
    for i, msg in enumerate(messages, 1):
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, str) and len(content) > 500:
            content = content[:500] + "..."
        lines.append(f"  {i}. [{role}] {content}")

    return "\n".join(lines)


__all__ = [
    "retrieve_relevant_messages",
    "should_skip_compaction",
    "build_retrieval_context_block",
    "is_enabled",
]
