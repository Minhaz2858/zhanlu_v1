"""Regression tests for Phase 5: NLU/Intent.

Covers:
- intent_planner.py keyword classification + stage mapping
- query_rewriter.py should_rewrite heuristic
"""

import pytest
from unittest.mock import patch


class TestIntentPlanner:
    """Verify EDIA 6-stage intent classification."""

    def test_keyword_classify_perception(self):
        from app.services.intent_planner import _keyword_classify
        stages = _keyword_classify("what is the current price?")
        assert stages is not None
        assert "perception" in stages

    def test_keyword_classify_forecast(self):
        from app.services.intent_planner import _keyword_classify
        stages = _keyword_classify("predict next week forecast trend outlook")
        assert stages is not None
        assert "forecast" in stages
        # forecast keywords score higher than perception
        assert stages[0] == "forecast"

    def test_keyword_classify_pricing(self):
        from app.services.intent_planner import _keyword_classify
        stages = _keyword_classify("what is the best pricing strategy and discount")
        assert stages is not None
        assert "pricing" in stages

    def test_keyword_classify_empty_returns_none(self):
        from app.services.intent_planner import _keyword_classify
        assert _keyword_classify("") is None

    def test_classify_intent_disabled_returns_default(self):
        from app.services.intent_planner import classify_intent
        with patch("app.services.intent_planner.is_enabled", return_value=False):
            result = classify_intent("forecast Q3 revenue")
            assert result.primary_stage == "perception"

    def test_stage_to_tools_returns_list(self):
        from app.services.intent_planner import stage_to_tools
        tools = stage_to_tools("forecast")
        assert isinstance(tools, list)
        assert len(tools) > 0
        assert "ask_forecast" in tools

    def test_stage_to_tools_unknown_falls_back(self):
        from app.services.intent_planner import stage_to_tools
        tools = stage_to_tools("nonexistent_stage")
        assert isinstance(tools, list)


class TestQueryRewriter:
    """Verify query rewriting heuristic."""

    def test_disabled_does_not_rewrite(self):
        from app.services.query_rewriter import should_rewrite
        with patch("app.services.query_rewriter.is_enabled", return_value=False):
            assert should_rewrite("tell me about the product") is False

    def test_short_query_needs_rewrite(self):
        from app.services.query_rewriter import should_rewrite
        with patch("app.services.query_rewriter.is_enabled", return_value=True):
            assert should_rewrite("what is it") is True

    def test_pronouns_trigger_rewrite(self):
        from app.services.query_rewriter import should_rewrite
        with patch("app.services.query_rewriter.is_enabled", return_value=True):
            assert should_rewrite("tell me more about this one") is True

    def test_rewrite_disabled_returns_unchanged(self):
        from app.services.query_rewriter import rewrite_query
        with patch("app.services.query_rewriter.is_enabled", return_value=False):
            result = rewrite_query("hello world")
            assert result.original == "hello world"
            assert result.rewritten == "hello world"
            assert result.changed is False

    def test_rewrite_no_llm_returns_unchanged(self):
        from app.services.query_rewriter import rewrite_query
        with patch("app.services.query_rewriter.is_enabled", return_value=True):
            result = rewrite_query("hello world", use_llm=False)
            assert result.changed is False
