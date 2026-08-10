"""Provider fallback -- switch to a fallback model on non-retryable errors.

When the error classifier (P1) returns ``should_fallback=True`` (e.g.
model_not_found), this module selects a fallback model from the configured
list and retries the LLM call.

The fallback list is configured via ``settings.LLM_FALLBACK_PROVIDERS`` (a
JSON array of ``{"name", "base_url", "api_key", "model"}`` objects).
``get_fallback_models()`` delegates to ``llm_service.get_llm_providers()``
and returns the list of ``LLMProvider`` objects.

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
    """Return the list of configured fallback model names.

    Delegates to ``llm_service.get_llm_providers()`` which properly
    parses the JSON array in ``LLM_FALLBACK_PROVIDERS``. Returns the
    ``model`` attribute of each provider (excluding primary).
    """
    from app.services.llm_service import get_llm_providers
    providers = get_llm_providers()
    # providers[0] is always primary — return only fallback model names
    return [p.model for p in providers[1:]]


async def with_fallback(
    primary_model: str,
    call_fn: Callable[[str], Awaitable[Any]],
    classified_error: ClassifiedError,
    *,
    user_fallback: str | None = None,
) -> tuple[Any | None, str | None]:
    """Try fallback models after a non-retryable error.

    Args:
        primary_model: The model that failed.
        call_fn: An async function that takes a model name and returns the
            LLM response. Will be called with each fallback model.
        classified_error: The classified error from the primary call.
        user_fallback: Optional user-configured fallback model (from
            ``UserSetting.fallback_model``). Prepended to the configured
            fallback chain — the user's preference is tried first.

    Returns:
        ``(response, fallback_model_used)`` on success, ``(None, None)`` if
        all fallbacks failed or none are configured.
    """
    if not classified_error.should_fallback:
        return None, None

    fallbacks = get_fallback_models()

    # Build the ordered chain: user fallback first, then system-configured models.
    chain: list[str] = []
    seen: set[str] = {primary_model}

    if user_fallback and isinstance(user_fallback, str):
        uf = user_fallback.strip()
        if uf and uf not in seen:
            chain.append(uf)
            seen.add(uf)

    for m in fallbacks:
        if m not in seen:
            chain.append(m)
            seen.add(m)

    if not chain:
        logger.warning(
            "Fallback needed (reason=%s) but no fallback models configured",
            classified_error.reason.value,
        )
        return None, None

    metrics.record_fallback()  # triggered

    for model in chain:
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
