"""Provider fallback -- switch to a fallback model on non-retryable errors.

When the error classifier (P1) returns ``should_fallback=True`` (e.g.
model_not_found), this module selects a fallback model from the configured
list and retries the LLM call.

The fallback list is configured via ``settings.LLM_FALLBACK_MODELS`` (a
comma-separated list of model names). If no fallbacks are configured, the
module is a no-op.

Inspired by Hermes' provider fallback pattern, adapted for Zhanlu's
single-provider OpenAI-compatible architecture.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Awaitable

from app.services.api_error_classifier import ClassifiedError
from app.services.agent_metrics import metrics

logger = logging.getLogger(__name__)


def get_fallback_models() -> list[str]:
    """Return the list of configured fallback model names."""
    from app.config import settings
    raw = getattr(settings, "LLM_FALLBACK_PROVIDERS", "") or ""
    if not raw:
        return []
    return [m.strip() for m in raw.split(",") if m.strip()]


async def with_fallback(
    primary_model: str,
    call_fn: Callable[[str], Awaitable[Any]],
    classified_error: ClassifiedError,
) -> tuple[Any | None, str | None]:
    """Try fallback models after a non-retryable error.

    Args:
        primary_model: The model that failed.
        call_fn: An async function that takes a model name and returns the
            LLM response. Will be called with each fallback model.
        classified_error: The classified error from the primary call.

    Returns:
        ``(response, fallback_model_used)`` on success, ``(None, None)`` if
        all fallbacks failed or none are configured.
    """
    if not classified_error.should_fallback:
        return None, None

    fallbacks = get_fallback_models()
    if not fallbacks:
        logger.warning(
            "Fallback needed (reason=%s) but no fallback models configured",
            classified_error.reason.value,
        )
        return None, None

    metrics.record_fallback()  # triggered

    for model in fallbacks:
        if model == primary_model:
            continue
        try:
            logger.info("Attempting fallback model: %s (reason=%s)", model, classified_error.reason.value)
            response = await call_fn(model)
            metrics.record_fallback(succeeded=True)
            logger.info("Fallback to %s succeeded", model)
            return response, model
        except Exception as e:
            logger.warning("Fallback to %s failed: %s", model, e)
            metrics.record_fallback(succeeded=False)
            continue

    logger.error("All fallback models failed")
    return None, None


__all__ = ["get_fallback_models", "with_fallback"]
