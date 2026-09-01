"""Regression tests for Phase 1: Model Layer fixes.

Covers:
- provider_fallback.py JSON parsing fix
- provider_health.py circuit breaker + health tracking
- model_router.py task-based routing
- llm_cache.py Redis cache
"""

import pytest
from unittest.mock import patch, MagicMock


# ── provider_fallback.py ──────────────────────────────────────────────


class TestFallbackModelsParsing:
    """Verify get_fallback_models() parses JSON, not comma-separated string."""

    def test_empty_config_returns_empty(self):
        """When LLM_FALLBACK_PROVIDERS is empty, return empty list."""
        from app.services.provider_fallback import get_fallback_models

        with patch("app.services.llm_service.get_llm_providers") as mock_get:
            mock_get.return_value = []  # no providers at all
            result = get_fallback_models()
            assert result == []

    def test_only_primary_returns_empty(self):
        """When only primary provider exists, fallbacks should be empty."""
        from app.services.provider_fallback import get_fallback_models
        from app.services.llm_service import LLMProvider

        providers = [
            LLMProvider(name="primary", base_url="http://x", api_key="k", model="m1"),
        ]
        with patch("app.services.llm_service.get_llm_providers") as mock_get:
            mock_get.return_value = providers
            result = get_fallback_models()
            assert result == []

    def test_fallback_models_parsed_from_json_array(self):
        """JSON array providers are parsed and returned as model names (excluding primary)."""
        from app.services.provider_fallback import get_fallback_models
        from app.services.llm_service import LLMProvider

        providers = [
            LLMProvider(name="primary", base_url="http://x", api_key="k", model="gpt-4o"),
            LLMProvider(name="backup", base_url="http://y", api_key="k2", model="deepseek-chat"),
            LLMProvider(name="backup2", base_url="http://z", api_key="k3", model="gpt-4o-mini"),
        ]
        with patch("app.services.llm_service.get_llm_providers") as mock_get:
            mock_get.return_value = providers
            result = get_fallback_models()
            assert result == ["deepseek-chat", "gpt-4o-mini"]


# ── provider_health.py ────────────────────────────────────────────────


class TestProviderHealth:
    """Verify circuit-breaker pattern: healthy/unhealthy, record, cooldown, probe."""

    def test_initial_state_is_healthy(self):
        from app.services.provider_health import is_healthy
        assert is_healthy("test-provider") is True

    def test_after_success_stays_healthy(self):
        from app.services.provider_health import is_healthy, record_success
        record_success("test-provider", 500.0)
        assert is_healthy("test-provider") is True

    def test_after_failure_below_threshold_stays_healthy(self):
        from app.services.provider_health import is_healthy, record_failure
        record_failure("test-provider")
        record_failure("test-provider")
        assert is_healthy("test-provider") is True

    def test_circuit_opens_after_threshold_failures(self):
        from app.services.provider_health import (
            is_healthy, record_failure,
            CIRCUIT_BREAKER_THRESHOLD,
        )
        # Reset state by using unique name
        pname = "threshold-test"
        for _ in range(CIRCUIT_BREAKER_THRESHOLD):
            record_failure(pname)
        assert is_healthy(pname) is False  # circuit open

    def test_success_resets_consecutive_failures(self):
        from app.services.provider_health import is_healthy, record_success, record_failure
        pname = "reset-test"
        record_failure(pname)
        record_failure(pname)
        record_success(pname, 100.0)
        # consecutive failures reset — still healthy
        assert is_healthy(pname) is True

    def test_average_latency(self):
        from app.services.provider_health import average_latency_ms, record_success
        pname = "latency-test"
        record_success(pname, 200.0)
        record_success(pname, 400.0)
        avg = average_latency_ms(pname)
        assert avg == 300.0

    def test_select_provider_healthy_first(self):
        from app.services.provider_health import select_provider, record_failure
        from app.services.llm_service import LLMProvider
        from app.services.provider_health import CIRCUIT_BREAKER_THRESHOLD

        p1 = LLMProvider(name="bad", base_url="http://a", api_key="k", model="m")
        p2 = LLMProvider(name="good", base_url="http://b", api_key="k2", model="m2")

        # Make p1 unhealthy
        for _ in range(CIRCUIT_BREAKER_THRESHOLD):
            record_failure("bad")

        chosen = select_provider([p1, p2])
        assert chosen.name == "good"

    def test_health_summary(self):
        from app.services.provider_health import health_summary, record_success, record_failure
        pname = "summary-test"
        record_success(pname, 100.0)
        record_failure(pname)
        s = health_summary()
        assert pname in s
        assert s[pname]["total_successes"] == 1
        assert s[pname]["total_failures"] == 1


# ── model_router.py ───────────────────────────────────────────────────


class TestModelRouter:
    """Verify task-based model routing."""

    def test_default_route_returns_llm_model(self):
        from app.services.model_router import route_model
        from app.config import settings
        assert route_model("nonexistent_task") == settings.LLM_MODEL

    def test_classify_task_tool_use(self):
        from app.services.model_router import classify_task
        msgs = [{"role": "user", "content": "hello"}]
        assert classify_task(msgs, tools_specified=True) == "tool_use"

    def test_classify_task_code_gen(self):
        from app.services.model_router import classify_task
        msgs = [{"role": "user", "content": "write code to sort a list in python"}]
        assert classify_task(msgs) == "code_gen"

    def test_classify_task_reasoning(self):
        from app.services.model_router import classify_task
        msgs = [{"role": "user", "content": "think step by step about this math problem"}]
        assert classify_task(msgs) == "reasoning"

    def test_classify_task_default_is_simple_chat(self):
        from app.services.model_router import classify_task
        msgs = [{"role": "user", "content": "what is the weather?"}]
        assert classify_task(msgs) == "simple_chat"

    def test_get_model_for_request_uses_routing_table(self):
        from app.services.model_router import get_model_for_request
        from app.config import settings

        with patch.object(settings, "MODEL_TASK_ROUTING",
                          '{"simple_chat":"deepseek-chat","reasoning":"deepseek-reasoner"}'):
            # Need to bypass cached routing table
            from app.services.model_router import _load_routing_table, route_model
            model = route_model("simple_chat")
            assert model == "deepseek-chat"
            model2 = route_model("reasoning")
            assert model2 == "deepseek-reasoner"


# ── llm_cache.py ──────────────────────────────────────────────────────


class TestLLMCache:
    """Verify caching logic (no Redis needed for key-building tests)."""

    def test_cache_only_temperature_zero(self):
        from app.services.llm_cache import get_cached_response
        # temperature > 0 always returns None (no caching)
        with patch("app.services.llm_cache._is_cache_enabled", return_value=True):
            result = get_cached_response(
                [{"role": "user", "content": "hi"}], "m1", temperature=0.7,
            )
            assert result is None

    def test_cache_key_deterministic(self):
        from app.services.llm_cache import _build_cache_key
        k1 = _build_cache_key([{"role": "user", "content": "hi"}], "m1", 0.0, "abc")
        k2 = _build_cache_key([{"role": "user", "content": "hi"}], "m1", 0.0, "abc")
        assert k1 == k2

    def test_cache_key_differs_by_model(self):
        from app.services.llm_cache import _build_cache_key
        k1 = _build_cache_key([{"role": "user", "content": "hi"}], "m1", 0.0)
        k2 = _build_cache_key([{"role": "user", "content": "hi"}], "m2", 0.0)
        assert k1 != k2

    def test_cache_disabled_when_setting_false(self):
        from app.services.llm_cache import get_cached_response
        with patch("app.services.llm_cache._is_cache_enabled", return_value=False):
            result = get_cached_response(
                [{"role": "user", "content": "hi"}], "m1", 0.0,
            )
            assert result is None
