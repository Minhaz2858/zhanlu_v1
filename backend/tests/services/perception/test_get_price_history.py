"""Tests for PerceptionService._get_price_history.

Verifies it delegates to the real market_dashboard_service.get_price_history
(pid, days=90) so the IsolationForest anomaly detection has real history.
"""
from __future__ import annotations

from unittest.mock import patch

from app.services.perception.service import PerceptionService


def test_get_price_history_delegates_to_market_dashboard_service():
    fake_hist = [("2026-08-01", 100.0), ("2026-08-02", 101.0)]
    with patch(
        "app.services.perception.service.mds.get_price_history",
        return_value=fake_hist,
    ) as m:
        result = PerceptionService._get_price_history("cracked_c5")

    m.assert_called_once_with("cracked_c5", days=90)
    # The method normalizes the warehouse tuples into {"date","price"} dicts
    # which is what the IsolationForest path (_detect_anomalies) consumes.
    assert result == [
        {"date": "2026-08-01", "price": 100.0},
        {"date": "2026-08-02", "price": 101.0},
    ]


def test_get_price_history_handles_missing_module_gracefully():
    with patch(
        "app.services.perception.service.mds.get_price_history",
        side_effect=AttributeError("no such function"),
    ):
        # Must degrade to [] not raise — anomaly detection simply skips.
        result = PerceptionService._get_price_history("cracked_c5")
    assert result == []
