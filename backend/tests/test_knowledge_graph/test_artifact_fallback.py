"""Tests for the artifact-aware empty-content fallback in agents.py.

When the LLM loop ends with empty assistant content but an artifact WAS
produced, the fallback must reference the artifact instead of the generic
"I gathered some information..." message.
"""

from __future__ import annotations

from app.routers.agents import (
    _artifact_aware_fallback,
    _collect_artifact_titles,
    _GENERIC_EMPTY_CONTENT_FALLBACK,
)


class TestArtifactAwareFallback:
    def test_single_artifact_title(self):
        titles = _collect_artifact_titles([], [{"title": "Weekly Market Report"}])
        assert titles == ["Weekly Market Report"]
        msg = _artifact_aware_fallback(titles)
        assert "Weekly Market Report" in msg
        # Must NOT fall back to the generic failure copy.
        assert _GENERIC_EMPTY_CONTENT_FALLBACK not in msg

    def test_dashboard_artifact_from_tool_calls(self):
        tcs = [
            {
                "name": "create_dashboard",
                "results": {"artifact": {"title": "Sales Dashboard"}},
            }
        ]
        titles = _collect_artifact_titles(tcs, [])
        assert "Sales Dashboard" in titles
        msg = _artifact_aware_fallback(titles)
        assert "Sales Dashboard" in msg

    def test_create_artifact_tool_result(self):
        tcs = [
            {
                "name": "create_artifact",
                "results": {"title": "Q3 Forecast Deck"},
            }
        ]
        titles = _collect_artifact_titles(tcs, [])
        assert "Q3 Forecast Deck" in titles

    def test_multiple_artifacts_joined(self):
        titles = _collect_artifact_titles(
            [],
            [{"title": "Report A"}, {"title": "Report B"}],
        )
        msg = _artifact_aware_fallback(titles)
        assert "Report A" in msg and "Report B" in msg

    def test_dedup_titles(self):
        titles = _collect_artifact_titles(
            [{"name": "create_artifact", "results": {"title": "Dup"}}],
            [{"title": "Dup"}],
        )
        assert titles == ["Dup"]

    def test_no_artifact_returns_generic(self):
        # When nothing was produced, the generic fallback is the right message.
        titles = _collect_artifact_titles([], [])
        assert titles == []
        # Caller decides: empty titles → caller uses generic fallback.
        # _artifact_aware_fallback is only invoked when titles exist.
        assert _artifact_aware_fallback(["x"]) != _GENERIC_EMPTY_CONTENT_FALLBACK
