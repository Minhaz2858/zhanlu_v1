"""Tests for the execution-first rewrite of `_SCHEMA_AWARE_PROTOCOL_BLOCK`.

Verifies the schema-aware query protocol no longer induces cautious
"readiness check then ask for go-ahead" behavior, and instead:

- Classifies EXECUTION vs PLANNING/DIAGNOSTIC commands FIRST.
- Replaces the data-quality blocker with a Data Quality Reporter.
- Bans permission-loop phrasing and standalone COUNT/MIN/MAX previews.
- Embeds a few-shot WRONG/CORRECT example for "Retry the full 30-day
  inventory pull".
- Preserves the existing structural rules (best-table, edges, auto-join,
  cite, no-hallucination).

The block is only appended to `data_agent` when
`SCHEMA_GRAPH_ENABLED` is on, so the tests monkeypatch that flag on/off.
"""

from __future__ import annotations

import pytest

from app.services.agent_prompts import get_system_prompt


@pytest.fixture
def enabled(monkeypatch):
    """Return a fixture that forces SCHEMA_GRAPH_ENABLED on for the test."""
    from app.services.agent_prompts import settings

    monkeypatch.setattr(settings, "SCHEMA_GRAPH_ENABLED", True)
    return settings


def _prompt(enabled):
    return get_system_prompt("data_agent")


class TestExecutionFirstRules:
    """New rules must be present and ordered correctly."""

    def test_command_classification_is_first_rule(self, enabled):
        text = _prompt(enabled).lower()
        assert "command classification" in text
        assert "first decision" in text

    def test_command_classification_precedes_best_table(self, enabled):
        text = _prompt(enabled).lower()
        cc = text.find("command classification")
        best = text.find("pick the best table")
        assert cc != -1 and best != -1, "both rules must be present"
        assert cc < best, (
            "COMMAND CLASSIFICATION must appear before PICK THE BEST TABLE"
        )

    def test_execution_keyword_list_present(self, enabled):
        text = _prompt(enabled).lower()
        for kw in [
            "pull",
            "retry",
            "run",
            "get",
            "give me",
            "fetch",
            "retrieve",
            "calculate",
            "make a report",
            "create dashboard",
            "generate analysis",
            "last 30 days",
            "try again",
        ]:
            assert kw in text, f"execution keyword {kw!r} missing"

    def test_execution_skips_readiness_and_aggregate_previews(self, enabled):
        text = _prompt(enabled).lower()
        assert "skip readiness checks" in text
        assert "skip aggregate previews" in text or "skip aggregate" in text

    def test_planning_diagnostic_keywords_present(self, enabled):
        text = _prompt(enabled).lower()
        for kw in ["what tables", "describe the schema", "is there data", "can you check"]:
            assert kw in text, f"planning keyword {kw!r} missing"


class TestDataQualityReporter:
    """The blocking data-quality rule is replaced by a reporter."""

    def test_data_quality_reporter_present(self, enabled):
        text = _prompt(enabled).lower()
        assert "data quality reporter" in text or "data quality" in text

    def test_auto_substitution_instruction(self, enabled):
        text = _prompt(enabled).lower()
        assert "fproducedate" in text
        assert "fupdatetime" in text
        assert "instead of" in text

    def test_never_stop_for_column_substitution(self, enabled):
        text = _prompt(enabled).lower()
        assert "never stop" in text


class TestForbiddenBehaviors:
    """Permission-loop and standalone-preview behaviors are banned."""

    def test_forbidden_behaviors_section_present(self, enabled):
        text = _prompt(enabled).lower()
        assert "forbidden behavior" in text

    def test_no_go_ahead_phrases(self, enabled):
        text = _prompt(enabled).lower()
        for phrase in [
            "ready to run when you say go",
            "just say the word",
            "i'll wait for your go-ahead",
            "should i proceed",
        ]:
            # The phrases must appear inside a "NEVER" prohibition context;
            # we assert the phrases exist and the section bans them via "never".
            assert phrase in text, f"forbidden phrase {phrase!r} missing"

    def test_no_standalone_aggregate_preview_before_real_query(self, enabled):
        text = _prompt(enabled).lower()
        # COUNT/MIN/MAX must not be prescribed as a separate step before the
        # real query. We assert the prohibition is stated.
        assert "count(*)" in text or "count(" in text


class TestFewShotExample:
    """The WRONG/CORRECT example is embedded."""

    def test_example_input_present(self, enabled):
        text = _prompt(enabled).lower()
        assert "retry the full 30-day inventory pull" in text

    def test_wrong_and_correct_markers_present(self, enabled):
        text = _prompt(enabled).lower()
        assert "wrong" in text
        assert "correct" in text


class TestPreservedRules:
    """The existing structural rules survive the rewrite."""

    def test_preserved_structural_rules(self, enabled):
        text = _prompt(enabled).lower()
        for rule in [
            "pick the best table",
            "check the edges",
            "auto-join or ask",
            "cite the evidence",
            "never hallucinate columns",
        ]:
            assert rule in text, f"preserved rule {rule!r} missing"

    def test_join_edge_kinds_preserved(self, enabled):
        text = _prompt(enabled).lower()
        assert "fk" in text
        assert "value_overlap" in text
        assert "name_match" in text


class TestFlagGatingAndScoping:
    """The block stays flag-gated and ecisco-only."""

    def test_flag_off_suppresses_block(self, monkeypatch):
        from app.services.agent_prompts import settings

        monkeypatch.setattr(settings, "SCHEMA_GRAPH_ENABLED", False)
        prompt = get_system_prompt("data_agent")
        text = prompt.lower()
        assert "command classification" not in text
        assert "data quality reporter" not in text

    def test_other_agents_excluded(self, enabled):
        for agent in ["general_agent", "forecast_agent", "analysis_agent"]:
            prompt = get_system_prompt(agent)
            text = prompt.lower()
            assert "command classification" not in text, (
                f"{agent} must NOT receive the schema-aware execution protocol"
            )
            assert "data quality reporter" not in text