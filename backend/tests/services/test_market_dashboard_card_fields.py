"""market_dashboard_service — 7-day summary-card fields on MarketSnapshot.

Covers:
- price_7d_high / price_7d_low computed from the existing history rows (no DB).
- sales_7d_tons / sales_7d_change_pct populated for ERP-mapped products and
  gracefully None for unmapped products or when the ERP query fails.
"""
import pytest

from app.services import market_dashboard_service as svc


def _enable_edia(monkeypatch):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x",
    )
    monkeypatch.setattr(svc, "is_edia_reachable_cached", lambda: True)


def test_snapshot_computes_7d_high_low_from_history(monkeypatch):
    """High/low come from the last-7d window of the history rows (no query)."""
    _enable_edia(monkeypatch)
    monkeypatch.setattr(svc, "_fetch_sales_7d", lambda pid: (None, None))

    def mock_history(pid):
        if pid == "naphtha":
            return [
                ("2026-07-22", 500.0),   # outside 7d window (ref = 07-30)
                ("2026-07-24", 590.0),
                ("2026-07-28", 605.0),
                ("2026-07-30", 612.5),
            ]
        return []

    monkeypatch.setattr(svc, "_read_product_history_rows", mock_history)
    snap = svc.get_market_snapshot(group="upstream")
    naphtha = next(s for s in snap if s.id == "naphtha")
    assert naphtha.price_7d_high == 612.5
    assert naphtha.price_7d_low == 590.0


def test_snapshot_populates_sales_7d_from_helper(monkeypatch):
    """ERP-mapped product forwards the helper's sales values onto the snapshot."""
    _enable_edia(monkeypatch)
    monkeypatch.setattr(svc, "_fetch_sales_7d",
                        lambda pid: (1411.7, -54.18))

    def mock_history(pid):
        if pid == "sis":
            return [("2026-07-29", 11000.0), ("2026-07-30", 11200.0)]
        return []

    monkeypatch.setattr(svc, "_read_product_history_rows", mock_history)

    snap = svc.get_market_snapshot(group="downstream")
    sis = next(s for s in snap if s.id == "sis")
    assert sis.sales_7d_tons == 1411.7
    assert sis.sales_7d_change_pct == -54.18


def test_snapshot_degrades_to_none_when_helper_fails(monkeypatch):
    """When the ERP query raises, fields stay None instead of erroring out."""
    _enable_edia(monkeypatch)

    def boom(pid):
        raise RuntimeError("erp down")

    monkeypatch.setattr(svc, "_fetch_sales_7d", boom)

    def mock_history(pid):
        if pid == "dcpd":
            return [("2026-07-29", 6100.0), ("2026-07-30", 6150.0)]
        return []

    monkeypatch.setattr(svc, "_read_product_history_rows", mock_history)

    snap = svc.get_market_snapshot(group="midstream")
    dcpd = next(s for s in snap if s.id == "dcpd")
    assert dcpd.sales_7d_tons is None
    assert dcpd.sales_7d_change_pct is None
    assert dcpd.price_7d_high == 6150.0
    assert dcpd.price_7d_low == 6100.0


def test_fetch_sales_7d_unmapped_returns_none(monkeypatch):
    """Products without ERP material names short-circuit to (None, None)."""
    _enable_edia(monkeypatch)
    # crude_oil / naphtha are upstream and not ERP-mapped.
    assert svc._fetch_sales_7d("crude_oil") == (None, None)


def test_fetch_sales_7d_no_engine_returns_none(monkeypatch):
    """No EDIA engine → (None, None) without raising."""
    _enable_edia(monkeypatch)
    monkeypatch.setattr(svc, "get_mysql_engine", lambda: None)
    # sis is ERP-mapped, but without an engine it must degrade.
    assert svc._fetch_sales_7d("sis") == (None, None)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
