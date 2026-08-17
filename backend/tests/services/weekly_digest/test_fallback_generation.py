"""Tests for deterministic fallback generation + sanitizer in weekly_digest."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from app.services.weekly_digest.generator import (
    _fallback_market_analysis,
    _fallback_ai_decision,
    _sanitize_llm_text,
    _generate_market_analysis,
    _generate_ai_decision,
)


FORECAST = {"7d": {"base": 6400.0, "bull": 6500.0, "bear": 6300.0, "confidence": 0.85}}


def test_fallback_market_analysis_heading_and_real_numbers():
    result = _fallback_market_analysis(
        "石脑油", 6300.0, 1.25,
        [{"date": "2026-07-20", "price": 6000.0}, {"date": "2026-08-16", "price": 6300.0}],
        FORECAST,
    )
    assert result.startswith("【石脑油市场分析】")
    assert "6000" in result
    assert "6300" in result
    assert "6400" in result
    assert "6500" in result


def test_fallback_market_analysis_no_data_still_non_empty():
    result = _fallback_market_analysis("加氢碳九", None, None, [], None)
    assert result.startswith("【加氢碳九市场分析】")
    assert "暂无价格数据" in result
    assert result.strip() != ""


def test_fallback_ai_decision_three_lines():
    result = _fallback_ai_decision("石脑油", 6300.0, 1.25, FORECAST)
    assert "策略建议" in result
    assert "依据" in result
    assert "关键风险" in result
    assert "6300" in result
    assert "6400" in result


def test_sanitize_strips_markdown():
    text = "**近期走势：** 价格上涨。\n## 驱动因素\n- 供给偏紧\n1. 需求回暖"
    result = _sanitize_llm_text(text)
    assert "**" not in result
    assert "近期走势" in result
    assert "供给偏紧" in result
    assert "需求回暖" in result


def test_market_analysis_falls_back_on_empty_llm():
    async def _run():
        with patch("app.services.llm_service.call_llm",
                   new=AsyncMock(return_value={"response": ""})):
            return await _generate_market_analysis(
                "石脑油",
                [{"date": "2026-08-16", "price": 6300.0}],
                6300.0, 1.25, FORECAST,
            )
    result = asyncio.run(_run())
    assert result.startswith("【石脑油市场分析】")
    assert "6300" in result


def test_market_analysis_falls_back_on_llm_exception():
    async def _run():
        with patch("app.services.llm_service.call_llm",
                   new=AsyncMock(side_effect=RuntimeError("boom"))):
            return await _generate_market_analysis("石脑油", [], None, None, None)
    result = asyncio.run(_run())
    assert result.startswith("【石脑油市场分析】")
    assert "暂无价格数据" in result


def test_ai_decision_falls_back_on_empty_llm():
    async def _run():
        with patch("app.services.llm_service.call_llm",
                   new=AsyncMock(return_value={"response": ""})):
            return await _generate_ai_decision("石脑油", 6300.0, 1.25, FORECAST, "analysis")
    result = asyncio.run(_run())
    assert "策略建议" in result
    assert "依据" in result
    assert "关键风险" in result
