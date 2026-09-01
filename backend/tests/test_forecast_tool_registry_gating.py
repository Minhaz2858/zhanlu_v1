"""Smoke test: heavy forecast tools must be enabled in the registry.

The forecast_agent subagent declares these tools in its tools list, but if
the registry has them gated off (enabled_by_default=False), the subagent
cannot use them. This test guards against that regression.
"""

from app.services.tool_handlers import forecast_tool  # noqa: F401
from app.services.tool_registry import registry


def test_forecast_run_is_enabled_by_default():
    entry = registry.get_entry("forecast_run")
    assert entry is not None, "forecast_run not registered"
    assert entry.enabled_by_default is True, (
        "forecast_run must be enabled_by_default=True so the forecast_agent "
        "subagent can call it. (Was: False — the original bug.)"
    )


def test_forecast_discover_is_enabled_by_default():
    entry = registry.get_entry("forecast_discover")
    assert entry is not None, "forecast_discover not registered"
    assert entry.enabled_by_default is True, (
        "forecast_discover must be enabled_by_default=True so the forecast_agent "
        "subagent can call it. (Was: False — the original bug.)"
    )


def test_lightweight_forecast_tools_remain_enabled():
    """The 3 already-enabled tools should stay enabled."""
    for name in ("forecast_get", "forecast_accuracy", "forecast_rules"):
        entry = registry.get_entry(name)
        assert entry is not None, f"{name} not registered"
        assert entry.enabled_by_default is True, f"{name} must stay enabled"
