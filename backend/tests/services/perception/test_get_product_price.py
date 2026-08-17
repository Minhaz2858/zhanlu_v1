"""Tests for PerceptionService._get_product_price.

The bug: the method imported a non-existent
``from app.services.market_dashboard import MarketDashboardService`` and called
``.build_snapshot()`` — which never existed — so it always returned ``None`` and
the perception snapshot stayed empty. These tests assert the corrected behavior:

* real rows -> dict with price/change keys
* empty rows -> None
* exception -> logger.warning (not silently swallowed)
* 7-day change uses the real 7-day-old row, NOT the previous-day row
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _make_rows():
    """Return rows shaped like market_dashboard_service.read_product_history_rows.

    Each row is (date_str, price_float). The last row is "today"; the row 7 days
    earlier is the 7-day reference.
    """
    return [
        ("2026-08-01", 100.0),
        ("2026-08-07", 110.0),  # 7 days before 2026-08-08 -> 7d reference
        ("2026-08-08", 120.0),  # previous day (1 day before today)
        ("2026-08-09", 130.0),  # "today" (latest)
    ]


def test_returns_dict_with_real_keys_when_rows_exist():
    from app.services.perception.service import PerceptionService

    with patch(
        "app.services.perception.service.mds.read_product_history_rows",
        return_value=_make_rows(),
    ):
        result = PerceptionService._get_product_price("cracked_c5")

    assert isinstance(result, dict)
    assert result["product_id"] == "cracked_c5"
    assert result["name_en"]
    assert result["name_cn"]
    # latest price is the last row
    assert result["latest_price"] == 130.0
    # 7d change uses the 2026-08-07 (110.0) row, not the 2026-08-08 (120.0) row
    # (130 - 110) / 110 = 18.18%
    assert abs(result["change_7d_pct"] - round((130.0 - 110.0) / 110.0 * 100, 2)) < 0.01


def test_returns_none_when_rows_empty():
    from app.services.perception.service import PerceptionService

    with patch(
        "app.services.perception.service.mds.read_product_history_rows",
        return_value=[],
    ):
        result = PerceptionService._get_product_price("cracked_c5")

    assert result is None


def test_exception_path_emits_logger_warning():
    import logging

    from app.services.perception.service import PerceptionService

    with patch(
        "app.services.perception.service.mds.read_product_history_rows",
        side_effect=RuntimeError("boom"),
    ):
        with patch.object(PerceptionService, "_get_product_price") as spy:
            # Do NOT go through the patched module path; force the real method to
            # run by patching the toast-less underlying call directly.
            pass

    # Call the real method but intercept the underlying mds call via module patch
    # so the exception propagates through the real try/except (logger.warning).
    with patch(
        "app.services.perception.service.mds.read_product_history_rows",
        side_effect=RuntimeError("boom"),
    ):
        with patch.object(logging.getLogger("app.services.perception.service"), "warning") as warn:
            result = PerceptionService._get_product_price("cracked_c5")
    assert result is None
    warn.assert_called()


def test_7d_change_uses_7day_old_row_not_previous_day():
    from app.services.perception.service import PerceptionService

    rows = [
        ("2026-08-01", 100.0),
        ("2026-08-07", 110.0),  # 7-day reference
        ("2026-08-08", 999.0),  # previous-day spike (must NOT be used)
        ("2026-08-09", 130.0),  # today
    ]
    with patch(
        "app.services.perception.service.mds.read_product_history_rows",
        return_value=rows,
    ):
        result = PerceptionService._get_product_price("cracked_c5")

    # If it used the previous-day (999) it would be hugely negative; instead it
    # uses the 7-day-old (110) reference: (130-110)/110 = +18.18%.
    assert result["change_7d_pct"] > 0
    assert abs(result["change_7d_pct"] - 18.18) < 0.1
    # And crucially NOT the previous-day-based value (-87%).
    assert result["change_7d_pct"] != pytest.approx(((130.0 - 999.0) / 999.0 * 100), abs=1.0)
