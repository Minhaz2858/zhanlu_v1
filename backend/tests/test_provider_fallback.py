"""Tests for provider fallback."""
import asyncio
import os
import sys
from unittest.mock import patch

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.api_error_classifier import ClassifiedError, FailoverReason
from app.services.provider_fallback import get_fallback_models, with_fallback


def test_get_fallback_models_empty():
    """Returns empty list when no fallback models configured."""
    with patch("app.config.settings.LLM_FALLBACK_PROVIDERS", ""):
        assert get_fallback_models() == []


def test_get_fallback_models_configured():
    """Returns list of model names from config."""
    with patch("app.config.settings.LLM_FALLBACK_PROVIDERS", "model-a, model-b, model-c"):
        models = get_fallback_models()
        assert models == ["model-a", "model-b", "model-c"]


def test_with_fallback_no_fallback_needed():
    """Returns (None, None) when should_fallback is False."""
    ce = ClassifiedError(reason=FailoverReason.rate_limit, retryable=True)
    result = asyncio.run(_test_no_fallback(ce))
    assert result == (None, None)


async def _test_no_fallback(ce):
    async def call_fn(model):
        return f"response from {model}"
    return await with_fallback("primary", call_fn, ce)


def test_with_fallback_no_models_configured():
    """Returns (None, None) when no fallback models configured."""
    ce = ClassifiedError(reason=FailoverReason.model_not_found, should_fallback=True)
    with patch("app.config.settings.LLM_FALLBACK_PROVIDERS", ""):
        result = asyncio.run(_test_no_models(ce))
    assert result == (None, None)


async def _test_no_models(ce):
    async def call_fn(model):
        return f"response from {model}"
    return await with_fallback("primary", call_fn, ce)


def test_with_fallback_succeeds():
    """Falls back to the first available model."""
    ce = ClassifiedError(reason=FailoverReason.model_not_found, should_fallback=True)
    with patch("app.config.settings.LLM_FALLBACK_PROVIDERS", "fallback-1, fallback-2"):
        result = asyncio.run(_test_fallback_success(ce))
    response, model = result
    assert response == "response from fallback-1"
    assert model == "fallback-1"


async def _test_fallback_success(ce):
    async def call_fn(model):
        return f"response from {model}"
    return await with_fallback("primary", call_fn, ce)


def test_with_fallback_skips_primary():
    """Doesn't retry the primary model."""
    ce = ClassifiedError(reason=FailoverReason.model_not_found, should_fallback=True)
    with patch("app.config.settings.LLM_FALLBACK_PROVIDERS", "primary, fallback-1"):
        result = asyncio.run(_test_skip_primary(ce))
    response, model = result
    assert model == "fallback-1"
    assert response == "response from fallback-1"


async def _test_skip_primary(ce):
    calls = []
    async def call_fn(model):
        calls.append(model)
        return f"response from {model}"
    result = await with_fallback("primary", call_fn, ce)
    assert "primary" not in calls  # primary was skipped
    return result


def test_with_fallback_all_fail():
    """Returns (None, None) when all fallbacks fail."""
    ce = ClassifiedError(reason=FailoverReason.model_not_found, should_fallback=True)
    with patch("app.config.settings.LLM_FALLBACK_PROVIDERS", "fallback-1, fallback-2"):
        result = asyncio.run(_test_all_fail(ce))
    assert result == (None, None)


async def _test_all_fail(ce):
    async def call_fn(model):
        raise Exception(f"{model} also failed")
    return await with_fallback("primary", call_fn, ce)


def test_with_fallback_first_fails_second_succeeds():
    """Tries next fallback if the first one fails."""
    ce = ClassifiedError(reason=FailoverReason.model_not_found, should_fallback=True)
    with patch("app.config.settings.LLM_FALLBACK_PROVIDERS", "fallback-1, fallback-2"):
        result = asyncio.run(_test_first_fails(ce))
    response, model = result
    assert model == "fallback-2"
    assert response == "response from fallback-2"


async def _test_first_fails(ce):
    call_count = 0
    async def call_fn(model):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("fallback-1 failed")
        return f"response from {model}"
    return await with_fallback("primary", call_fn, ce)
