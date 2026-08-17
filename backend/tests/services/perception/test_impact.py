"""Tests for the shared impact module (services/perception/impact.py).

Verifies the ¥/day formula for price/arbitrage alerts, graceful degradation
when ERP volume is missing, and ERP-down behavior.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.perception import impact


class _Row:
    def __init__(self, total_tons):
        self.total_tons = total_tons


def _make_mysql_engine(total_tons: float | None):
    """Fake MySQL engine whose connect() yields a conn returning total_tons."""
    engine = MagicMock()
    conn = engine.connect.return_value.__enter__.return_value
    result = MagicMock()
    result.fetchone.return_value = (
        _Row(total_tons) if total_tons is not None else None
    )
    conn.execute.return_value = result
    return engine, conn


_PRICE_ALERT = {
    "product_id": "c5_resin",
    "product_name": "C5石油树脂",
    "alert_type": "price_spike",
    "severity": "high",
    "price": 9900.0,
    "change_7d_pct": 16.82,
}

_ARBITRAGE_ALERT = {
    "product_id": "naphtha",
    "product_name": "石脑油",
    "alert_type": "regional_arbitrage",
    "price": 1200.0,
    "freight_cost": 150.0,
}


def test_price_spike_impact_formula():
    """|price × change%| ÷ 7 × avg_daily_volume_tons."""
    engine, _ = _make_mysql_engine(3600.0)  # 3600 tons / 30d = 120 tons/day
    with patch("app.services.perception.impact.get_mysql_engine", return_value=engine):
        result = impact.compute_impact(_PRICE_ALERT)

    expected = abs(9900.0 * 16.82 / 100) / 7 * 120.0
    assert result["impact_yuan_per_day"] == expected
    assert result["avg_daily_volume_tons"] == 120.0


def test_price_drop_uses_abs_value():
    """Negative change must still yield a positive ¥/day impact."""
    engine, _ = _make_mysql_engine(2100.0)  # 70 tons/day
    alert = {**_PRICE_ALERT, "alert_type": "price_drop", "change_7d_pct": -8.4}
    with patch("app.services.perception.impact.get_mysql_engine", return_value=engine):
        result = impact.compute_impact(alert)

    expected = abs(9900.0 * 8.4 / 100) / 7 * 70.0
    assert result["impact_yuan_per_day"] == expected


def test_arbitrage_impact_formula():
    """(spread − freight) × avg_daily_volume_tons."""
    engine, _ = _make_mysql_engine(3000.0)  # 100 tons/day
    with patch("app.services.perception.impact.get_mysql_engine", return_value=engine):
        result = impact.compute_impact(_ARBITRAGE_ALERT)

    assert result["impact_yuan_per_day"] == (1200.0 - 150.0) * 100.0


def test_missing_volume_returns_null_impact():
    """No ERP volume → impact_yuan_per_day None, alert still representable."""
    with patch("app.services.perception.impact.get_mysql_engine", return_value=None):
        result = impact.compute_impact(_PRICE_ALERT)

    assert result["impact_yuan_per_day"] is None
    assert result["avg_daily_volume_tons"] is None


def test_erp_down_returns_null_impact():
    """MySQL connection failure → graceful None, no exception propagates."""
    engine = MagicMock()
    engine.connect.side_effect = RuntimeError("warehouse down")
    with patch("app.services.perception.impact.get_mysql_engine", return_value=engine):
        result = impact.compute_impact(_PRICE_ALERT)

    assert result["impact_yuan_per_day"] is None


def test_empty_alert_returns_null_impact():
    result = impact.compute_impact({})
    assert result["impact_yuan_per_day"] is None
    assert result["avg_daily_volume_tons"] is None
