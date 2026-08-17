"""Tests for the decision tool's perception bridge.

Mirror of the pricing-tool regression: decision/tool.py imported a non-existent
``MarketDashboardService(db).build_snapshot()`` and produced an empty
``perception={}`` dict. The fix calls ``build_perception_snapshot`` from the
perception module to obtain the full snapshot.

These tests assert the corrected path calls ``build_perception_snapshot``.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_ask_decision_calls_build_perception_snapshot():
    import asyncio

    from app.services.decision import tool as decision_tool

    args = {
        "product": "cracked_c5",
        # No perception pre-supplied -> handler must build it from the pipeline.
    }
    db = MagicMock()

    with patch(
        "app.services.decision.tool.resolve_product_id",
        return_value="cracked_c5",
    ), patch(
        "app.services.perception.service.build_perception_snapshot",
        return_value={"products": {"cracked_c5": {"latest_price": 130.0}}},
    ) as m:
        # Run the async handler but tolerate downstream (assemble/critic) errors
        # — we only care the perception path executed.
        try:
            asyncio.run(decision_tool._handle_ask_decision(args, db))
        except Exception as exc:
            if "build_perception_snapshot" not in str(exc):
                raise

    m.assert_called_once()
