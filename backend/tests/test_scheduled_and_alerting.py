"""Tests for scheduled tasks and alerting."""
import asyncio
import os
import sys
from unittest.mock import patch, MagicMock, AsyncMock

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.alerting import (
    AlertThreshold,
    DEFAULT_THRESHOLDS,
    check_alerts,
)
from app.services.agent_metrics import AgentMetrics, metrics
from app.services.scheduled_tasks import (
    MEMORY_CONSOLIDATION_INTERVAL,
    SKILL_CURATION_INTERVAL,
    ALERT_CHECK_INTERVAL,
    start_scheduled_tasks,
    stop_scheduled_tasks,
)


# -- Alerting tests --

def test_check_alerts_no_alerts_when_clean():
    """No alerts when all metrics are below thresholds."""
    # Reset metrics to clean state
    original = metrics.get_snapshot()
    metrics.reset()
    alerts = check_alerts()
    assert alerts == []
    # Restore (don't pollute other tests)
    metrics.reset()


def test_check_alerts_triggers_on_high_guardrail_halt():
    """Alert triggers when guardrail halt count exceeds threshold."""
    metrics.reset()
    for _ in range(15):
        metrics.record_guardrail_halt("same_tool_failure_halt")

    alerts = check_alerts()
    assert len(alerts) >= 1
    assert any("guardrail" in a.lower() for a in alerts)
    metrics.reset()


def test_check_alerts_triggers_on_budget_exhaustion():
    """Alert triggers when budget exhaustion count exceeds threshold."""
    metrics.reset()
    for _ in range(25):
        metrics.record_budget(10, 10)  # exhausted

    alerts = check_alerts()
    assert any("budget" in a.lower() for a in alerts)
    metrics.reset()


def test_check_alerts_triggers_on_rate_limit():
    """Alert triggers when rate_limit error count exceeds threshold."""
    metrics.reset()
    for _ in range(25):
        metrics.record_error("rate_limit")

    alerts = check_alerts()
    assert any("rate_limit" in a.lower() or "rate limit" in a.lower() for a in alerts)
    metrics.reset()


def test_alert_threshold_check_exceeds():
    """AlertThreshold.check returns message when value exceeds threshold."""
    threshold = AlertThreshold(
        name="test", description="test alert",
        metric_path="iteration_budget.exhausted_count",
        threshold=5,
    )
    snapshot = {"iteration_budget": {"exhausted_count": 10}}
    msg = threshold.check(snapshot)
    assert msg is not None
    assert "test" in msg
    assert "current=10" in msg


def test_alert_threshold_check_below():
    """AlertThreshold.check returns None when value is below threshold."""
    threshold = AlertThreshold(
        name="test", description="test alert",
        metric_path="iteration_budget.exhausted_count",
        threshold=20,
    )
    snapshot = {"iteration_budget": {"exhausted_count": 5}}
    msg = threshold.check(snapshot)
    assert msg is None


def test_alert_threshold_check_missing_path():
    """AlertThreshold.check returns None when path doesn't exist."""
    threshold = AlertThreshold(
        name="test", description="test alert",
        metric_path="nonexistent.path.here",
        threshold=1,
    )
    snapshot = {"iteration_budget": {"exhausted_count": 5}}
    msg = threshold.check(snapshot)
    assert msg is None


def test_default_thresholds_count():
    """There are at least 5 default alert thresholds."""
    assert len(DEFAULT_THRESHOLDS) >= 5


def test_custom_thresholds():
    """check_alerts accepts custom thresholds."""
    custom = [
        AlertThreshold(
            name="custom", description="custom alert",
            metric_path="background_review.spawned",
            threshold=2,
        ),
    ]
    metrics.reset()
    metrics.record_background_review("spawned")
    metrics.record_background_review("spawned")
    metrics.record_background_review("spawned")

    alerts = check_alerts(custom)
    assert len(alerts) == 1
    assert "custom" in alerts[0]
    metrics.reset()


# -- Scheduled task tests --

def test_schedule_intervals():
    """Schedule intervals are reasonable values."""
    assert MEMORY_CONSOLIDATION_INTERVAL >= 60  # at least 1 minute
    assert SKILL_CURATION_INTERVAL > MEMORY_CONSOLIDATION_INTERVAL
    assert ALERT_CHECK_INTERVAL < MEMORY_CONSOLIDATION_INTERVAL


def test_start_scheduled_tasks_no_loop():
    """start_scheduled_tasks returns without error when no event loop."""
    # Outside an event loop
    start_scheduled_tasks()
    # Should not raise — just logs a warning


def test_start_stop_scheduled_tasks_with_loop():
    """start and stop scheduled tasks within an event loop."""
    async def _test():
        start_scheduled_tasks()
        # Give tasks a tiny moment to register
        await asyncio.sleep(0.01)
        await stop_scheduled_tasks()

    asyncio.run(_test())
