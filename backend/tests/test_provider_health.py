"""Regression tests for provider_health.py (Part 2 — Phase 1 model layer)."""

import time
from unittest.mock import patch

from app.services import provider_health as ph


def _reset_state():
    """Reset all provider state between tests."""
    ph._providers.clear()


class TestRecordSuccess:
    """Tests for record_success."""

    def test_resets_consecutive_failures(self):
        """record_success should zero out consecutive_failures."""
        _reset_state()
        ph.record_failure("openai")
        ph.record_failure("openai")
        assert ph._providers["openai"].consecutive_failures == 2
        ph.record_success("openai", 150.0)
        assert ph._providers["openai"].consecutive_failures == 0
        assert ph._providers["openai"].total_successes == 1

    def test_updates_latency_and_timestamp(self):
        """record_success should accumulate latency and update last_success_at."""
        _reset_state()
        now = time.monotonic()
        with patch("time.monotonic", return_value=now):
            ph.record_success("openai", 200.0)
        st = ph._providers["openai"]
        assert st.total_latency_ms == 200.0
        assert st.last_success_at == now

    def test_disables_probe_mode(self):
        """record_success should reset probe_allowed to False."""
        _reset_state()
        st = ph._ensure_state("openai")
        st.probe_allowed = True
        ph.record_success("openai", 100.0)
        assert ph._providers["openai"].probe_allowed is False


class TestRecordFailure:
    """Tests for record_failure."""

    def test_increments_failure_counters(self):
        """record_failure should increment both counters."""
        _reset_state()
        ph.record_failure("deepseek")
        st = ph._providers["deepseek"]
        assert st.consecutive_failures == 1
        assert st.total_failures == 1

    def test_opens_circuit_after_threshold(self):
        """Circuit breaker opens after CIRCUIT_BREAKER_THRESHOLD consecutive failures."""
        _reset_state()
        now = 100.0
        for i in range(ph.CIRCUIT_BREAKER_THRESHOLD):
            with patch("time.monotonic", return_value=now + i):
                ph.record_failure("deepseek")
        st = ph._providers["deepseek"]
        assert st.consecutive_failures == ph.CIRCUIT_BREAKER_THRESHOLD
        assert st.circuit_open_at > 0
        assert st.probe_allowed is False

    def test_circuit_does_not_reopen_when_already_open(self):
        """circuit_open_at should not reset when circuit is already open."""
        _reset_state()
        open_time = time.monotonic()
        with patch("time.monotonic", return_value=open_time):
            for _ in range(ph.CIRCUIT_BREAKER_THRESHOLD):
                ph.record_failure("openai")
        with patch("time.monotonic", return_value=open_time + 5):
            ph.record_failure("openai")
        assert ph._providers["openai"].circuit_open_at == open_time


class TestIsHealthy:
    """Tests for is_healthy."""

    def test_healthy_when_below_threshold(self):
        """Provider with fewer failures than threshold is healthy."""
        _reset_state()
        ph.record_failure("openai")
        assert ph.is_healthy("openai") is True

    def test_unhealthy_during_cooldown(self):
        """Provider with circuit open and within cooldown is unhealthy."""
        _reset_state()
        now = time.monotonic()
        with patch("time.monotonic", return_value=now):
            for _ in range(ph.CIRCUIT_BREAKER_THRESHOLD):
                ph.record_failure("openai")
        with patch("time.monotonic", return_value=now + ph.CIRCUIT_BREAKER_COOLDOWN_S - 1):
            assert ph.is_healthy("openai") is False

    def test_healthy_after_cooldown_expires(self):
        """After cooldown expires, provider is healthy again (resets for probe)."""
        _reset_state()
        now = time.monotonic()
        with patch("time.monotonic", return_value=now):
            for _ in range(ph.CIRCUIT_BREAKER_THRESHOLD):
                ph.record_failure("openai")
        with patch("time.monotonic", return_value=now + ph.CIRCUIT_BREAKER_COOLDOWN_S + 1):
            assert ph.is_healthy("openai") is True


class TestAverageLatency:
    """Tests for average_latency_ms."""

    def test_zero_for_no_successes(self):
        _reset_state()
        assert ph.average_latency_ms("new_provider") == 0.0

    def test_correct_average(self):
        _reset_state()
        ph.record_success("openai", 100.0)
        ph.record_success("openai", 200.0)
        ph.record_success("openai", 300.0)
        assert ph.average_latency_ms("openai") == 200.0

    def test_failures_do_not_affect_latency(self):
        _reset_state()
        ph.record_success("openai", 500.0)
        ph.record_failure("openai")
        assert ph.average_latency_ms("openai") == 500.0


class TestHealthSummary:
    """Tests for health_summary."""

    def test_returns_dict_with_expected_keys(self):
        _reset_state()
        ph.record_success("openai", 100.0)
        summary = ph.health_summary()
        assert "openai" in summary
        entry = summary["openai"]
        assert "consecutive_failures" in entry
        assert "total_successes" in entry
        assert "total_failures" in entry
        assert "avg_latency_ms" in entry
        assert "healthy" in entry
        assert "circuit_open" in entry

    def test_empty_when_no_providers(self):
        _reset_state()
        assert ph.health_summary() == {}
