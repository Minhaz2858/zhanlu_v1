"""Regression tests for intent_planner.py (Part 2 — Phase 5 NLU / intent)."""

from unittest.mock import patch

from app.services.intent_planner import (
    IntentResult,
    _keyword_classify,
    classify_intent,
    is_enabled,
    stage_to_tools,
    STAGES,
)


class TestKeywordClassify:
    """Tests for _keyword_classify (fast fallback, returns list[str] or None)."""

    def test_forecast_keyword(self):
        result = _keyword_classify("predict next quarter revenue")
        assert result is not None
        assert "forecast" in result

    def test_pricing_keyword(self):
        result = _keyword_classify("what should we price this at")
        assert result is not None
        assert "pricing" in result

    def test_decision_keyword(self):
        result = _keyword_classify("should I buy more inventory")
        assert result is not None
        assert "decision" in result

    def test_what_if_keyword(self):
        result = _keyword_classify("what if brent rises 5%")
        assert result is not None
        assert "what_if" in result

    def test_what_if_scenario_keyword(self):
        result = _keyword_classify("simulate a naphtha +3% shock")
        assert result is not None
        assert "what_if" in result

    def test_diagnosis_keyword(self):
        result = _keyword_classify("why did sales drop")
        assert result is not None
        assert "diagnosis" in result

    def test_perception_keyword(self):
        result = _keyword_classify("what is the current status of C5")
        assert result is not None
        assert "perception" in result

    def test_no_match_returns_none(self):
        assert _keyword_classify("hello how are you") is None

    def test_empty_text(self):
        assert _keyword_classify("") is None

    def test_multiple_matches_returns_multiple_stages(self):
        result = _keyword_classify("forecast the price and decide what to do")
        assert result is not None
        assert "forecast" in result

    def test_case_insensitive(self):
        result = _keyword_classify("FORECAST QUARTERLY REVENUE")
        assert result is not None
        assert "forecast" in result


class TestIntentResult:
    """Tests for IntentResult dataclass."""

    def test_default_values(self):
        ir = IntentResult()
        assert ir.stages == []
        assert ir.primary_stage == "perception"
        assert ir.confidence == 0.5
        assert ir.rationale == ""
        assert ir.needs_disambiguation is False

    def test_to_dict(self):
        ir = IntentResult(
            stages=["forecast", "pricing"],
            primary_stage="forecast",
            confidence=0.85,
            rationale="user asked for prediction",
            needs_disambiguation=False,
            disambiguation_question="",
        )
        d = ir.to_dict()
        assert d["stages"] == ["forecast", "pricing"]
        assert d["primary_stage"] == "forecast"
        assert d["confidence"] == 0.85
        assert d["rationale"] == "user asked for prediction"
        assert d["needs_disambiguation"] is False

    def test_to_dict_disambiguation(self):
        ir = IntentResult(
            stages=["resolve_product"],
            primary_stage="resolve_product",
            needs_disambiguation=True,
            disambiguation_question="Which C5 model?",
        )
        d = ir.to_dict()
        assert d["disambiguation_question"] == "Which C5 model?"


class TestClassifyIntent:
    """Tests for classify_intent (top-level entry)."""

    def test_returns_default_when_disabled(self):
        with patch("app.services.intent_planner.is_enabled", return_value=False):
            result = classify_intent("forecast revenue")
            assert result.primary_stage == "perception"
            assert result.stages == ["perception"]

    def test_keyword_classify_when_enabled(self):
        with patch("app.services.intent_planner.is_enabled", return_value=True):
            result = classify_intent("forecast next month sales")
            assert result is not None
            # Keyword classify should match "forecast"
            assert result.primary_stage in ("forecast", "pricing", "decision")

    def test_empty_message_returns_default(self):
        with patch("app.services.intent_planner.is_enabled", return_value=True):
            result = classify_intent("")
            assert result.primary_stage == "perception"
            assert result.confidence == 0.2

    def test_llm_classify_flag(self):
        """use_llm=True should not crash even without real LLM (falls back to keyword)."""
        with patch("app.services.intent_planner.is_enabled", return_value=True):
            result = classify_intent("forecast revenue", use_llm=True)
            assert isinstance(result, IntentResult)
            assert result.primary_stage in STAGES


class TestStageToTools:
    """Tests for stage_to_tools."""

    def test_known_stage_returns_list(self):
        tools = stage_to_tools("forecast")
        assert isinstance(tools, list)
        assert len(tools) > 0

    def test_unknown_stage_returns_perception_tools(self):
        tools = stage_to_tools("nonexistent")
        assert tools == stage_to_tools("perception")

    def test_perception_stage_has_tools(self):
        tools = stage_to_tools("perception")
        assert "ask_perception" in tools or "web_search" in tools

    def test_what_if_stage_has_tools(self):
        tools = stage_to_tools("what_if")
        assert "forecast_what_if" in tools


class TestIsEnabled:
    """Tests for is_enabled."""

    def test_default_false(self):
        mock_settings = type("S", (), {"INTENT_PLANNER_ENABLED": False})()
        with patch("app.config.settings", mock_settings):
            assert is_enabled() is False

    def test_true_when_enabled(self):
        mock_settings = type("S", (), {"INTENT_PLANNER_ENABLED": True})()
        with patch("app.config.settings", mock_settings):
            assert is_enabled() is True
