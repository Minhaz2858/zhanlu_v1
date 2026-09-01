"""Tests for the artifact-aware empty-content fallback in agents.py.

When the LLM loop ends with empty assistant content but an artifact WAS
produced, the fallback must reference the artifact instead of the generic
"I gathered some information..." message.
"""

from __future__ import annotations

import json

from app.routers.agents import (
    _artifact_aware_fallback,
    _choose_fallback,
    _collect_artifact_titles,
    _compact_report_card,
    _condense_data_agent_results,
    _data_summary_fallback,
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


class TestFixAReportCardTitles:
    """Fix A: _collect_artifact_titles must surface data-agent report cards."""

    def test_report_card_payload_title(self):
        tcs = [
            {
                "name": "ask_data_agent",
                "results": {
                    "report_card_payload": {
                        "title": "Shipment Volume by Region",
                        "kpis": [],
                    }
                },
            }
        ]
        titles = _collect_artifact_titles(tcs, [])
        assert "Shipment Volume by Region" in titles

    def test_summary_title_fallback(self):
        tcs = [
            {
                "name": "ask_data_agent",
                "results": {"summary": {"title": "Demand Forecast"}},
            }
        ]
        titles = _collect_artifact_titles(tcs, [])
        assert "Demand Forecast" in titles


class TestFixBChooseFallback:
    """Fix B: _choose_fallback picks the most specific message."""

    def test_report_card_prefers_data_summary_fallback(self):
        tcs = [
            {
                "name": "ask_data_agent",
                "results": {
                    "report_card_payload": {"title": "Margin Analysis"}
                },
            }
        ]
        msg = _choose_fallback(tcs, [], user_content="")
        # Data-summary copy names the report and offers a combined dashboard.
        assert "Margin Analysis" in msg
        assert _GENERIC_EMPTY_CONTENT_FALLBACK not in msg
        # No misleading dashboard redirect ("create dashboard") when the user
        # never asked for a dashboard.
        assert "create dashboard" not in msg.lower()

    def test_artifact_fallback_when_only_artifact(self):
        tcs = [{"name": "create_artifact", "results": {"title": "Q3 Deck"}}]
        msg = _choose_fallback(tcs, [], user_content="")
        assert "Q3 Deck" in msg

    def test_dashboard_redirect_only_when_requested(self):
        tcs = []
        msg = _choose_fallback([], [], user_content="build me a dashboard")
        assert "create dashboard" in msg.lower()

    def test_generic_when_nothing_produced(self):
        msg = _choose_fallback([], [], user_content="hello")
        assert msg == _GENERIC_EMPTY_CONTENT_FALLBACK


class TestFixDContextCondensing:
    """Fix D: _condense_data_agent_results shrinks oversized report cards."""

    @staticmethod
    def _big_rcp_result(title="Big Report"):
        # Summary must be long enough that the serialized payload exceeds the
        # 6000-char condensation threshold in _condense_data_agent_results.
        rcp = {
            "title": title,
            "summary": "s" * 6000,
            "kpis": [
                {"label": f"k{i}", "value": str(i)} for i in range(10)
            ],
            "chart": {
                "title": title + " chart",
                "type": "bar",
                "data": [{"label": f"row{i}", "value": i} for i in range(50)],
            },
            "insights": [
                {"text": f"insight {i}"} for i in range(5)
            ],
            "actions": [{"label": "a1", "prompt": "p"}],
        }
        return json.dumps({"report_card_payload": rcp})

    def test_oversized_data_result_is_condensed(self):
        big = self._big_rcp_result()
        assert len(big) > 6000
        messages = [{"role": "tool", "tool_call_id": "t1", "content": big}]
        _condense_data_agent_results(messages)
        out = messages[0]["content"]
        assert len(out) < 1200
        assert "Big Report" in out
        assert "insight 0" in out
        # KPI digest present, but truncated to top 6 with ellipsis marker.
        assert "k0=0" in out and "…" in out

    def test_condensation_idempotent_when_always_on(self):
        # Fix 1 runs condensation before EVERY LLM call, not just the
        # finish line. A second call must be a no-op (idempotent).
        big = self._big_rcp_result()
        messages = [{"role": "tool", "tool_call_id": "t1", "content": big}]
        _condense_data_agent_results(messages)
        condensed = messages[0]["content"]
        _condense_data_agent_results(messages)
        assert messages[0]["content"] == condensed
        assert len(condensed) < 1200
        assert "Big Report" in condensed

    def test_small_result_left_untouched(self):
        small = json.dumps({"report_card_payload": {"title": "Tiny"}})
        messages = [{"role": "tool", "tool_call_id": "t1", "content": small}]
        _condense_data_agent_results(messages)
        assert messages[0]["content"] == small

    def test_non_data_result_left_untouched(self):
        big = "x" * 9000  # huge but not a data-agent report card
        messages = [{"role": "tool", "tool_call_id": "t1", "content": big}]
        _condense_data_agent_results(messages)
        assert messages[0]["content"] == big

    def test_non_json_result_left_untouched(self):
        messages = [{"role": "tool", "tool_call_id": "t1", "content": "z" * 7000}]
        _condense_data_agent_results(messages)
        assert len(messages[0]["content"]) == 7000

    def test_compact_report_card_shape(self):
        rcp = {
            "title": "T",
            "summary": "Sum",
            "kpis": [{"label": "a", "value": "1"}],
            "chart": {"title": "C", "type": "line", "data": [{"label": "x", "value": 5}]},
            "insights": [{"text": "i"}],
            "actions": [{"label": "act", "prompt": "p"}],
        }
        out = _compact_report_card(rcp)
        assert "T" in out and "a=1" in out and "insights" in out.lower()
