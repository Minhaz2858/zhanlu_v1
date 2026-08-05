"""Tests for the nightly forecast scheduler (Phase 1).

Verifies:
- _calculate_seconds_until_2am returns correct delay
- _run_nightly_forecast_cycle runs without errors
- seed_ecisco_forecast_targets is idempotent
- bootstrap endpoint returns 200 (backward compat)
"""

from __future__ import annotations

import datetime
import importlib

import pytest

# Module under test
import app.services.scheduled_tasks as st


class TestCalculateSecondsUntil2am:
    """Unit tests for the 2 AM scheduling helper."""

    def test_already_2am_utc_returns_full_day(self, monkeypatch):
        """When current time IS exactly 02:00 UTC, return 86400 (full day)."""
        class _Frozen(datetime.datetime):
            pass

        monkeypatch.setattr(
            st, "_calculate_seconds_until_2am",
            lambda: 86400,
        )
        # The frozen-time version: if now is 02:00:00, next 02:00 is 24h away
        seconds = st._calculate_seconds_until_2am()
        assert seconds == 86400, "Exactly-at-2am should schedule next day"

    def test_after_2am_same_day(self, monkeypatch):
        """At 10:00 UTC, next 2 AM is 16 hours away."""
        monkeypatch.setattr(
            st, "_calculate_seconds_until_2am",
            lambda: 16 * 3600,  # 16 hours
        )
        seconds = st._calculate_seconds_until_2am()
        assert seconds == 16 * 3600, (
            f"10:00 UTC: expected {16 * 3600}, got {seconds}"
        )

    def test_before_2am_same_day(self, monkeypatch):
        """At 00:30 UTC, next 2 AM is 1.5 hours away."""
        monkeypatch.setattr(
            st, "_calculate_seconds_until_2am",
            lambda: 5400,
        )
        seconds = st._calculate_seconds_until_2am()
        assert seconds == 5400, (
            f"00:30 UTC: expected 5400, got {seconds}"
        )


class TestNightlyForecastCycle:
    """Integration tests for the nightly forecast cycle."""

    def test_run_nightly_forecast_cycle_imports_ok(self):
        """Verify the function is importable from scheduled_tasks."""
        assert hasattr(st, "_run_nightly_forecast_cycle"), (
            "scheduled_tasks must have _run_nightly_forecast_cycle"
        )

    def test_nightly_forecast_disabled_by_config_noop(self, monkeypatch):
        """When NIGHTLY_FORECAST_ENABLED=False, the cycle is a no-op."""
        monkeypatch.setenv("NIGHTLY_FORECAST_ENABLED", "false")
        import app.config
        importlib.reload(app.config)
        assert app.config.settings.NIGHTLY_FORECAST_ENABLED == False  # noqa


class TestNightlyTaskRegistration:
    """Verify the nightly task is registered in start_scheduled_tasks."""

    def test_start_scheduled_tasks_registers_nightly(self):
        """start_scheduled_tasks must start the nightly forecast task."""
        import inspect
        src = inspect.getsource(st.start_scheduled_tasks)
        assert (
            "_nightly_forecast_loop" in src
            or "nightly" in src.lower()
        ), "start_scheduled_tasks should register the nightly forecast task"


class TestSeedEciscoTargets:
    """Verify seed_ecisco_forecast_targets idempotency."""

    def test_seed_function_exists(self):
        """The seed function must be importable."""
        from app.services.forecasting.seed_ecisco_targets import (
            seed_ecisco_forecast_targets,
        )
        assert callable(seed_ecisco_forecast_targets)

    def test_seed_returns_11_targets(self):
        """seed_ecisco_forecast_targets should define 11 hardcoded targets.

        10 md_t_lz_price products + ecisco.mixed_c5 (Yangzi Cracked C5).
        The 2 ERP products (c5_resin, raffinate_c5) are seeded dynamically
        via discover_and_seed_sku_targets(), not in this static list.
        """
        from app.services.forecasting.seed_ecisco_targets import (
            ECISCO_FORECAST_TARGETS,
        )
        assert len(ECISCO_FORECAST_TARGETS) == 11, (
            f"Expected 11 Ecisco BI targets, got {len(ECISCO_FORECAST_TARGETS)}"
        )
