"""Tests for Phase 2: Chatbot forecast agent reads cached DB results first.

Verifies:
- FORECAST_AGENT_PROMPT instructs the agent to always use forecast_get first
- forecast_agent definition includes both forecast_get and forecast_run tools
"""

from __future__ import annotations

import pytest

from app.services.agent_definitions import (
    FORECAST_AGENT_PROMPT,
    list_agent_definitions,
)  # noqa: E402


class TestForecastAgentPrompt:
    """Verify the forecast agent system prompt guides to cache-first behavior."""

    def test_prompt_instructs_forecast_get_first(self):
        """The prompt must contain a hard rule to call forecast_get first."""
        text = FORECAST_AGENT_PROMPT.lower()
        # At least one of these keyword-pair indicators must be present
        indicators = [
            ("forecast_get", "first"),
            ("forecast_get", "before"),
            ("forecast_get", "cache"),
            ("forecast_get", "prefer"),
            ("forecast_get", "always"),
        ]
        found = any(
            w1 in text and w2 in text
            for w1, w2 in indicators
        )
        assert found, (
            "FORECAST_AGENT_PROMPT must instruct the agent to use "
            "forecast_get first (before forecast_run)"
        )

    def test_prompt_mentions_stale_threshold(self):
        """The prompt should mention when to consider a forecast as stale."""
        text = FORECAST_AGENT_PROMPT.lower()
        assert "24" in text or "stale" in text or (
            "hour" in text
        ), "Prompt should define a staleness threshold"

    def test_prompt_mentions_refresh_trigger(self):
        """The prompt should list words that trigger a fresh forecast_run."""
        text = FORECAST_AGENT_PROMPT.lower()
        triggers = ["refresh", "re-run", "update", "rerun", "fresh"]
        found = any(t in text for t in triggers)
        assert found, (
            "Prompt should mention refresh triggers "
            "(e.g., 'refresh', 're-run', 'update')"
        )


class TestForecastAgentTools:
    """Verify the forecast agent has the correct tools configured."""

    def test_forecast_agent_has_forecast_get(self):
        """forecast_agent must have forecast_get in its tool list."""
        defs = list_agent_definitions()
        fa = next((d for d in defs if d.name == "forecast_agent"), None)
        assert fa is not None, "forecast_agent not found in definitions"
        tools = fa.tools or []
        assert "forecast_get" in tools, (
            f"forecast_get missing from forecast_agent tools: {tools}"
        )

    def test_forecast_agent_has_forecast_run(self):
        """forecast_agent must still have forecast_run for explicit refresh."""
        defs = list_agent_definitions()
        fa = next((d for d in defs if d.name == "forecast_agent"), None)
        assert fa is not None, "forecast_agent not found in definitions"
        tools = fa.tools or []
        assert "forecast_run" in tools, (
            f"forecast_run missing from forecast_agent tools: {tools}"
        )

    def test_forecast_agent_has_forecast_accuracy(self):
        """forecast_agent must have forecast_accuracy for metrics."""
        defs = list_agent_definitions()
        fa = next((d for d in defs if d.name == "forecast_agent"), None)
        tools = fa.tools or []
        assert "forecast_accuracy" in tools, "forecast_accuracy tool required"
