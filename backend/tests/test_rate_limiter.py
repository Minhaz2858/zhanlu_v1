"""Regression tests for rate_limiter.py (Part 2 — Phase 3 scalability)."""

from unittest.mock import MagicMock, patch

from app.services import rate_limiter as rl


def _reset_limiter():
    """Reset module-level Redis client cache so tests are isolated."""
    import app.services.rate_limiter as mod
    mod.__dict__.pop("_redis_client_cache", None)
    if hasattr(mod, "_redis_client"):
        delattr(mod, "_redis_client")


class TestCheckRateLimit:
    """Tests for check_rate_limit."""

    def test_allows_when_disabled(self):
        mock_settings = type("S", (), {"RATE_LIMIT_ENABLED": False})()
        with patch("app.services.rate_limiter._is_enabled", return_value=False):
            allowed, retry_after = rl.check_rate_limit("user-1")
            assert allowed is True
            assert retry_after == 0

    def test_allows_whitelist_user(self):
        with patch("app.services.rate_limiter._is_enabled", return_value=True):
            with patch("app.services.rate_limiter._whitelist", return_value={"admin-user", "supervisor"}):
                mock_redis = MagicMock()
                with patch("app.services.rate_limiter._get_redis_client", return_value=mock_redis):
                    allowed, _ = rl.check_rate_limit("admin-user")
                    assert allowed is True
                    mock_redis.zcard.assert_not_called()

    def test_allows_under_limit(self):
        mock_redis = MagicMock()
        mock_redis.zcard.return_value = 3  # well under max
        with patch("app.services.rate_limiter._is_enabled", return_value=True):
            with patch("app.services.rate_limiter._whitelist", return_value=set()):
                with patch("app.services.rate_limiter._get_redis_client", return_value=mock_redis):
                    allowed, retry_after = rl.check_rate_limit("user-1", max_requests=60)
                    assert allowed is True
                    assert retry_after == 0

    def test_rejects_over_limit(self):
        mock_redis = MagicMock()
        mock_redis.zcard.return_value = 10  # at limit when max=10
        mock_redis.zrange.return_value = [(b"", 100.0)]
        with patch("app.services.rate_limiter._is_enabled", return_value=True):
            with patch("app.services.rate_limiter._whitelist", return_value=set()):
                with patch("app.services.rate_limiter._get_redis_client", return_value=mock_redis):
                    with patch("time.monotonic", return_value=150.0):
                        allowed, retry_after = rl.check_rate_limit("user-1", max_requests=10, window_s=60)
                        assert allowed is False
                        assert retry_after > 0

    def test_redis_unavailable_allows_through(self):
        with patch("app.services.rate_limiter._is_enabled", return_value=True):
            with patch("app.services.rate_limiter._get_redis_client", return_value=None):
                allowed, _ = rl.check_rate_limit("user-1")
                assert allowed is True


class TestIsRateLimited:
    """Tests for is_rate_limited (shorthand)."""

    def test_false_when_not_limited(self):
        with patch("app.services.rate_limiter._is_enabled", return_value=False):
            assert rl.is_rate_limited("user-1") is False

    def test_true_when_limited(self):
        mock_redis = MagicMock()
        mock_redis.zcard.return_value = 999
        mock_redis.zrange.return_value = [(b"", 0.0)]
        with patch("app.services.rate_limiter._is_enabled", return_value=True):
            with patch("app.services.rate_limiter._whitelist", return_value=set()):
                with patch("app.services.rate_limiter._get_redis_client", return_value=mock_redis):
                    with patch("time.monotonic", return_value=999999.0):
                        assert rl.is_rate_limited("user-1") is True


class TestResetRateLimit:
    """Tests for reset_rate_limit."""

    def test_deletes_redis_key(self):
        mock_redis = MagicMock()
        mock_redis.delete.return_value = True
        with patch("app.services.rate_limiter._get_redis_client", return_value=mock_redis):
            result = rl.reset_rate_limit("user-1")
            assert result is True
            mock_redis.delete.assert_called_once()

    def test_handles_none_redis(self):
        with patch("app.services.rate_limiter._get_redis_client", return_value=None):
            result = rl.reset_rate_limit("user-1")
            assert result is False
