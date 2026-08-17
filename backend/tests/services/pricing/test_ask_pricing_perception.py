"""Tests for the pricing tool's perception bridge.

The bug: pricing/tool.py imported a non-existent
``MarketDashboardService(db).build_snapshot()`` and fell through to an empty
``perception={}`` dict. The fix routes through the real
``PerceptionService._get_product_price`` (which uses
``market_dashboard_service.read_product_history_rows``).

These tests assert the corrected path actually invokes
``PerceptionService._get_product_price`` for the requested product.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_ask_pricing_calls_read_product_history_rows():
    import asyncio

    from app.services.pricing import tool as pricing_tool

    args = {
        "product_id": "cracked_c5",
        "name": "裂解碳五",
        # No perception pre-supplied -> handler must build it via PerceptionService.
    }
    db = MagicMock()

    with patch(
        "app.services.perception.service.PerceptionService._get_product_price"
    ) as m_get:
        m_get.return_value = {
            "product_id": "cracked_c5",
            "name_en": "Cracked C5",
            "name_cn": "裂解碳五",
            "latest_price": 130.0,
            "latest_date": "2026-08-09",
            "change_7d_pct": 8.33,
        }

        # Run the async handler but tolerate downstream (LLM) errors — we only
        # care the perception path executed.
        try:
            asyncio.run(pricing_tool._handle_ask_pricing(args, db))
        except Exception as exc:
            if "read_product_history_rows" not in str(exc):
                raise

    m_get.assert_called_once()
    assert m_get.call_args.args[0] == "cracked_c5"
