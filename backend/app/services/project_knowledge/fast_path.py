"""Qwen pre-LLM answer router.

Used by the agent loop (routers/agents.py) to inject a cached answer
into the first LLM call's context when the model is Qwen and the cache
hits. Reduces LLM round-trips for product/entity/metric questions.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.config import settings

from .cache import ProjectKnowledgeCache, is_qwen_model
from .models import CacheQueryResult

logger = logging.getLogger(__name__)


def try_fast_path(
    db: Session,
    project_id: str,
    question: str,
    model_id: str | None,
) -> CacheQueryResult | None:
    """Return a CacheQueryResult if fast-path applies, else None.

    Fast-path applies when:
      - PROJECT_KNOWLEDGE_QWEN_FAST_PATH is True
      - model_id matches QWEN_FAST_PATH_MODEL_PREFIXES
      - the cache returns a non-None hit
    """
    if not getattr(settings, "PROJECT_KNOWLEDGE_QWEN_FAST_PATH", False):
        return None
    if not is_qwen_model(model_id):
        return None
    if not project_id or not question:
        return None
    try:
        cache = ProjectKnowledgeCache(project_id)
        return cache.query(db, question, model_id=model_id)
    except Exception as e:
        logger.debug("fast_path.try_fast_path failed (non-fatal): %s", e)
        return None


def build_cached_system_block(result: CacheQueryResult) -> str:
    """Render a CacheQueryResult as a system-prompt block for the LLM."""
    if result is None:
        return ""
    return (
        "<project_knowledge_cache>\n"
        f"kind: {result.kind}\n"
        f"confidence: {result.confidence}\n"
        f"{result.context_block}\n"
        "Use the data above as the authoritative answer. "
        "If the user's question is fully answered by this block, "
        "do NOT call ask_data_agent or any other tool -- return the "
        "answer directly. Otherwise fall through to your normal tool plan.\n"
        "</project_knowledge_cache>"
    )


__all__ = ["try_fast_path", "build_cached_system_block"]
