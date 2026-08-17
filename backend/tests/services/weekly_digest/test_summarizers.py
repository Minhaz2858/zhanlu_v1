"""Unit tests for weekly_digest price/forecast summarizer helpers."""
from __future__ import annotations

from app.services.weekly_digest.generator import (
    _summarize_prices,
    _summarize_forecast,
)


def test_summarize_prices_includes_start_end_30d_and_7d_pct():
    prices = [
        {"date": "2026-07-20", "price": 6000.0},
        {"date": "2026-08-10", "price": 6150.0},
        {"date": "2026-08-14", "price": 6220.0},
        {"date": "2026-08-15", "price": 6250.0},
        {"date": "2026-08-16", "price": 6300.0},
    ]
    result = _summarize_prices(prices)
    assert "6000" in result
    assert "6300" in result
    assert "+5.00%" in result
    assert "近7日涨跌幅" in result
    assert "5 个数据点" in result


def test_summarize_prices_empty_returns_placeholder():
    assert _summarize_prices([]) == "暂无价格数据"
    assert _summarize_prices([{"date": "2026-08-16", "price": None}]) == "暂无价格数据"


def test_summarize_forecast_includes_vs_current_delta():
    forecast = {
        "7d": {"base": 6400.0, "bull": 6500.0, "bear": 6300.0, "confidence": 0.85},
        "30d": {"base": 6600.0, "bull": 6800.0, "bear": 6400.0, "confidence": 0.65},
    }
    result = _summarize_forecast(forecast, current_price=6300.0)
    assert "6400" in result
    assert "6500" in result
    assert "6600" in result
    assert "+1.6%" in result
    assert "85%" in result


def test_summarize_forecast_empty_returns_placeholder():
    assert _summarize_forecast(None, 6300.0) == "暂无预测数据"
    assert _summarize_forecast({}, 6300.0) == "暂无预测数据"
