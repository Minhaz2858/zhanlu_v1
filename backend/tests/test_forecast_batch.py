"""Tests for /api/market-dashboard/forecast-base-batch endpoint (Task 2)."""
import pytest
from datetime import date


class FakeResult:
    def __init__(self, rows): self._rows = rows
    def fetchall(self): return self._rows
    def first(self): return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, rows): self._rows = rows
    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "MAX" in sql and "v_forecast_price" in sql:
            return FakeResult([(date(2026, 7, 30),)])
        # For detail query: return matching row by material_id
        if params:
            for r in self._rows:
                if str(r[0]) in str(params):
                    return FakeResult([r])
        return FakeResult(self._rows)
    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakeEngine:
    def __init__(self, rows): self._rows = rows
    def connect(self): return FakeConn(self._rows)


def test_forecast_batch_returns_predicted_price_for_mapped_product(monkeypatch):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x", raising=False,
    )
    monkeypatch.setattr(
        "app.services.market_dashboard_service.is_edia_reachable_cached",
        lambda: True,
    )
    rows = [(7000.0,)]
    monkeypatch.setattr(
        "app.services.market_dashboard_service.get_mysql_engine",
        lambda: FakeEngine(rows),
    )
    from app.services import market_dashboard_service as svc
    r = svc.get_forecast_base_batch(["cracked_c5"], day=7)
    assert r.day == 7
    assert r.base_by_product["cracked_c5"] == 7000.0


def test_forecast_batch_returns_null_for_unmapped_product(monkeypatch):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x", raising=False,
    )
    monkeypatch.setattr(
        "app.services.market_dashboard_service.is_edia_reachable_cached",
        lambda: True,
    )
    monkeypatch.setattr(
        "app.services.market_dashboard_service.get_mysql_engine",
        lambda: FakeEngine([]),
    )
    from app.services import market_dashboard_service as svc
    r = svc.get_forecast_base_batch(["crude_oil", "isoprene"], day=7)
    assert r.base_by_product["crude_oil"] is None
    assert r.base_by_product["isoprene"] is None
    assert r.day == 7
