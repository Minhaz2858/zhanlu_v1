"""Regression tests for the alert scan worker's org_id resolution + reachability.

The bug (fixed): ``run_alert_scan_cycle`` wrote ``org_id=None`` because it fell
back to ``settings.ORG_ID`` which does not exist — only ``settings.DEFAULT_ORG_ID``
is defined. That made every persisted alert invisible to the reader side (which
also resolved to ``None``) → the Market Alerts panel always showed "0 total".

These tests pin:
* the worker passes ``settings.DEFAULT_ORG_ID`` to ``AlertService.detect_and_persist``,
* the result dict includes ``reachable`` + ``reason`` so the UI can surface
  "warehouse unreachable" instead of silently showing zero.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _force_alerts_enabled(monkeypatch):
    """The worker early-returns when ECISCO_BI_ALERTS_ENABLED is off."""
    from app.config import settings

    monkeypatch.setattr(settings, "ECISCO_BI_ALERTS_ENABLED", True)


def _snapshot_with_product(price: float = 100.0, change: float = 5.0) -> dict:
    return {
        "products": {
            "cracked_c5": {
                "id": "cracked_c5",
                "name_en": "Cracked C5",
                "name_cn": "裂解C5",
                "latest_price": price,
                "change_7d_pct": change,
            }
        },
        "generated_at": "2026-08-17T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_scan_passes_default_org_id_to_detect_and_persist(monkeypatch):
    from app.config import settings
    from app.services.perception.worker import run_alert_scan_cycle

    with patch(
        "app.services.perception.worker.is_edia_reachable", return_value=True
    ), patch(
        "app.services.perception.worker.build_perception_snapshot",
        return_value=_snapshot_with_product(),
    ), patch(
        "app.services.perception.worker.AlertService.detect_and_persist",
        return_value=[{"id": "a1"}],
    ) as detect_mock, patch(
        "app.database.SessionLocal", return_value=MagicMock()
    ):
        result = await run_alert_scan_cycle()

    # The worker must hand detect_and_persist the DEFAULT_ORG_ID, never None.
    detect_mock.assert_called_once()
    args, _kwargs = detect_mock.call_args
    assert args[1] == settings.DEFAULT_ORG_ID
    assert args[1] is not None

    assert result["reachable"] is True
    assert result["new_alerts"] == 1
    assert result["total_scanned"] == 1
    assert result["error"] is None


@pytest.mark.asyncio
async def test_scan_reports_warehouse_unreachable_without_scanning():
    from app.services.perception.worker import run_alert_scan_cycle

    with patch(
        "app.services.perception.worker.is_edia_reachable", return_value=False
    ), patch(
        "app.services.perception.worker.build_perception_snapshot"
    ) as snapshot_mock:
        result = await run_alert_scan_cycle()

    assert result["reachable"] is False
    assert result["reason"] == "warehouse unreachable"
    assert result["total_scanned"] == 0
    assert result["new_alerts"] == 0
    # No snapshot work attempted when the warehouse is down.
    snapshot_mock.assert_not_called()


@pytest.mark.asyncio
async def test_scan_skips_when_flag_disabled(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ECISCO_BI_ALERTS_ENABLED", False)

    from app.services.perception.worker import run_alert_scan_cycle

    with patch(
        "app.services.perception.worker.is_edia_reachable", return_value=True
    ) as reachable_mock:
        result = await run_alert_scan_cycle()

    assert result["skipped"] is True
    reachable_mock.assert_not_called()
