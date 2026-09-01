"""Regression tests for llm_cache.py (Part 2 — Phase 1 model layer)."""

import json
from unittest.mock import MagicMock, patch

from app.services import llm_cache as lc


class TestBuildCacheKey:
    """Tests for _build_cache_key."""

    def test_deterministic_same_input(self):
        messages = [{"role": "user", "content": "hello"}]
        key1 = lc._build_cache_key(messages, "gpt-4", 0.0, "")
        key2 = lc._build_cache_key(messages, "gpt-4", 0.0, "")
        assert key1 == key2

    def test_different_messages_produce_different_key(self):
        key1 = lc._build_cache_key([{"role": "user", "content": "hello"}], "gpt-4", 0.0, "")
        key2 = lc._build_cache_key([{"role": "user", "content": "world"}], "gpt-4", 0.0, "")
        assert key1 != key2

    def test_different_model_produces_different_key(self):
        msgs = [{"role": "user", "content": "hello"}]
        key1 = lc._build_cache_key(msgs, "gpt-4", 0.0, "")
        key2 = lc._build_cache_key(msgs, "deepseek", 0.0, "")
        assert key1 != key2

    def test_different_schema_hash_produces_different_key(self):
        msgs = [{"role": "user", "content": "hello"}]
        key1 = lc._build_cache_key(msgs, "gpt-4", 0.0, "abc")
        key2 = lc._build_cache_key(msgs, "gpt-4", 0.0, "def")
        assert key1 != key2

    def test_key_has_correct_prefix(self):
        key = lc._build_cache_key([{"role": "user", "content": "hi"}], "gpt-4", 0.0, "")
        assert key.startswith(lc.CACHE_KEY_PREFIX)

    def test_temperature_rounding(self):
        """Temperature 0.001 rounds to 0.0; temperature 0.006 rounds to 0.01."""
        msgs = [{"role": "user", "content": "hi"}]
        key1 = lc._build_cache_key(msgs, "gpt-4", 0.001, "")
        key2 = lc._build_cache_key(msgs, "gpt-4", 0.0, "")
        assert key1 == key2


class TestGetCachedResponse:
    """Tests for get_cached_response."""

    def test_returns_none_when_cache_disabled(self):
        with patch.object(lc, "_is_cache_enabled", return_value=False):
            result = lc.get_cached_response(
                [{"role": "user", "content": "hi"}], "gpt-4", 0.0
            )
            assert result is None

    def test_returns_none_for_nonzero_temperature(self):
        with patch.object(lc, "_is_cache_enabled", return_value=True):
            result = lc.get_cached_response(
                [{"role": "user", "content": "hi"}], "gpt-4", 0.7
            )
            assert result is None

    def test_returns_none_when_redis_unavailable(self):
        with patch.object(lc, "_is_cache_enabled", return_value=True):
            with patch.object(lc, "_get_redis_client", return_value=None):
                result = lc.get_cached_response(
                    [{"role": "user", "content": "hi"}], "gpt-4", 0.0
                )
                assert result is None

    def test_returns_cached_value_on_hit(self):
        mock_redis = MagicMock()
        expected = {"response": "cached answer", "tokens": 5}
        mock_redis.get.return_value = json.dumps(expected)

        with patch.object(lc, "_is_cache_enabled", return_value=True):
            with patch.object(lc, "_get_redis_client", return_value=mock_redis):
                result = lc.get_cached_response(
                    [{"role": "user", "content": "hi"}], "gpt-4", 0.0
                )
                assert result == expected

    def test_returns_none_on_redis_miss(self):
        mock_redis = MagicMock()
        mock_redis.get.return_value = None

        with patch.object(lc, "_is_cache_enabled", return_value=True):
            with patch.object(lc, "_get_redis_client", return_value=mock_redis):
                result = lc.get_cached_response(
                    [{"role": "user", "content": "hi"}], "gpt-4", 0.0
                )
                assert result is None

    def test_returns_none_on_redis_error(self):
        mock_redis = MagicMock()
        mock_redis.get.side_effect = ConnectionError("redis down")

        with patch.object(lc, "_is_cache_enabled", return_value=True):
            with patch.object(lc, "_get_redis_client", return_value=mock_redis):
                result = lc.get_cached_response(
                    [{"role": "user", "content": "hi"}], "gpt-4", 0.0
                )
                assert result is None


class TestSetCachedResponse:
    """Tests for set_cached_response."""

    def test_noop_when_cache_disabled(self):
        mock_redis = MagicMock()
        with patch.object(lc, "_is_cache_enabled", return_value=False):
            with patch.object(lc, "_get_redis_client", return_value=mock_redis):
                lc.set_cached_response(
                    [{"role": "user", "content": "hi"}], "gpt-4", 0.0,
                    {"response": "answer"},
                )
                mock_redis.setex.assert_not_called()

    def test_noop_for_nonzero_temperature(self):
        mock_redis = MagicMock()
        with patch.object(lc, "_is_cache_enabled", return_value=True):
            with patch.object(lc, "_get_redis_client", return_value=mock_redis):
                lc.set_cached_response(
                    [{"role": "user", "content": "hi"}], "gpt-4", 0.7,
                    {"response": "answer"},
                )
                mock_redis.setex.assert_not_called()

    def test_noop_when_redis_unavailable(self):
        with patch.object(lc, "_is_cache_enabled", return_value=True):
            with patch.object(lc, "_get_redis_client", return_value=None):
                lc.set_cached_response(
                    [{"role": "user", "content": "hi"}], "gpt-4", 0.0,
                    {"response": "answer"},
                )

    def test_stores_response_on_hit(self):
        mock_redis = MagicMock()
        with patch.object(lc, "_is_cache_enabled", return_value=True):
            with patch.object(lc, "_get_redis_client", return_value=mock_redis):
                with patch.object(lc, "_cache_ttl", return_value=3600):
                    lc.set_cached_response(
                        [{"role": "user", "content": "hi"}], "gpt-4", 0.0,
                        {"response": "answer"}
                    )
                    mock_redis.setex.assert_called_once()
                    args, _ = mock_redis.setex.call_args
                    assert args[1] == 3600  # TTL

    def test_handles_redis_write_error_gracefully(self):
        mock_redis = MagicMock()
        mock_redis.setex.side_effect = ConnectionError("redis down")
        with patch.object(lc, "_is_cache_enabled", return_value=True):
            with patch.object(lc, "_get_redis_client", return_value=mock_redis):
                # Should not raise
                lc.set_cached_response(
                    [{"role": "user", "content": "hi"}], "gpt-4", 0.0,
                    {"response": "answer"},
                )
