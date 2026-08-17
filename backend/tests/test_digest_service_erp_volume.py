"""Tests for DigestService ERP volume lookup.

Regression: POST /api/alerts/digest returned 500. ``_get_avg_daily_volume``
queried ``erp_product_sales_details`` (an EDIA MySQL warehouse table) through
the Postgres ``self.db`` session with Postgres-only ``INTERVAL '30 days'``
syntax, raising ``ProgrammingError: relation does not exist``.

Fixed: routed through ``app.core.mysql_db.get_mysql_engine()`` against the
canonical ``erp_v_sale_orderentry`` view, matching products via
``_PRODUCT_MATERIAL_NAMES`` (the ERP table's ``FNAME`` is NULL / stale 2025).

Run:  pytest backend/tests/test_digest_service_erp_volume.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models.alert import AlertLog
from app.services.perception.digest_service import DigestService


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


_ALERT = {
    "product_id": "c5_resin",
    "product_name": "C5石油树脂",
    "alert_type": "price_spike",
    "severity": "high",
    "price": 9900.0,
    "change_7d_pct": 16.82,
}


def test_avg_daily_volume_queries_mysql_engine_not_postgres():
    """The ERP volume lookup must go through the MySQL warehouse engine."""
    engine, conn = _make_mysql_engine(3600.0)
    svc = DigestService(db=MagicMock(), org_id="org-1")

    with patch(
        "app.services.perception.impact.get_mysql_engine",
        return_value=engine,
    ):
        result = svc._get_avg_daily_volume(_ALERT)

    # 3600 tons / 30 days = 120 tons/day
    assert result == 120.0
    engine.connect.assert_called_once()
    # The Postgres session must never be used for ERP volume.
    svc.db.execute.assert_not_called()
    # Canonical table + MySQL-compatible SQL (no Postgres ::/interval).
    sql = str(conn.execute.call_args.args[0])
    assert "erp_v_sale_orderentry" in sql
    assert "FDELIQTY" in sql
    assert ":start_date" in sql


def test_avg_daily_volume_resolves_material_name_mapping():
    """c5_resin must be translated to 碳五石油树脂 via _PRODUCT_MATERIAL_NAMES."""
    engine, conn = _make_mysql_engine(600.0)
    svc = DigestService(db=MagicMock(), org_id="org-1")

    with patch(
        "app.services.perception.impact.get_mysql_engine",
        return_value=engine,
    ):
        svc._get_avg_daily_volume(_ALERT)

    params = conn.execute.call_args.args[1]
    assert params.get("name_0") == "碳五石油树脂"


def test_avg_daily_volume_returns_none_when_mysql_unconfigured():
    """Graceful degradation: no engine -> None, Postgres never touched."""
    svc = DigestService(db=MagicMock(), org_id="org-1")
    with patch(
        "app.services.perception.impact.get_mysql_engine",
        return_value=None,
    ):
        result = svc._get_avg_daily_volume(_ALERT)
    assert result is None
    svc.db.execute.assert_not_called()


def test_avg_daily_volume_none_for_empty_alert():
    svc = DigestService(db=MagicMock(), org_id="org-1")
    with patch(
        "app.services.perception.impact.get_mysql_engine",
        return_value=_make_mysql_engine(300.0)[0],
    ):
        assert svc._get_avg_daily_volume({}) is None


def test_avg_daily_volume_none_when_no_rows():
    engine, _ = _make_mysql_engine(None)
    svc = DigestService(db=MagicMock(), org_id="org-1")
    with patch(
        "app.services.perception.impact.get_mysql_engine",
        return_value=engine,
    ):
        assert svc._get_avg_daily_volume(_ALERT) is None


def _make_alert_row(product_id, alert_type, ts, alert_id="x"):
    return AlertLog(
        id=alert_id,
        org_id="org-1",
        product_id=product_id,
        product_name=product_id,
        alert_type=alert_type,
        severity="high",
        message="test",
        triggered_at=ts,
        read_at=None,
        dismissed_at=None,
        payload=None,
    )


@pytest.mark.asyncio
async def test_collect_dedups_same_product_and_type():
    """Repeated scans must not produce duplicate digest items."""
    t2 = datetime(2026, 8, 17, 14, 28, 33, tzinfo=timezone.utc)
    t1 = datetime(2026, 8, 17, 10, 4, 15, tzinfo=timezone.utc)

    # query().filter(...).filter(org).order_by().all() chain on a fake session
    fake_db = MagicMock()
    fake_query = MagicMock()
    fake_org_filtered = MagicMock()
    fake_db.query.return_value = fake_query
    # First .filter() (alert_type/window/dismissed) returns another filterable
    fake_query.filter.return_value = fake_org_filtered
    # Second .filter() (org_id) returns the terminal query object
    fake_org_filtered.filter.return_value.order_by.return_value.all.return_value = [
        _make_alert_row("c5_resin", "price_spike", t2, "aeb9d7d0"),
        _make_alert_row("c5_resin", "price_spike", t1, "02d5dee2"),
        _make_alert_row("naphtha", "price_drop", t1, "c703d409"),
    ]

    svc = DigestService(db=fake_db, org_id="org-1")
    items = await svc.collect()

    assert len(items) == 2
    keys = [(it["product_id"], it["alert_type"]) for it in items]
    assert keys == [("c5_resin", "price_spike"), ("naphtha", "price_drop")]
    # The kept c5_resin item is the most recent one.
    kept = next(it for it in items if it["product_id"] == "c5_resin")
    assert kept["id"] == "aeb9d7d0"


def test_compute_impact_uses_avg_volume():
    """compute_impact must incorporate the ERP volume into ¥/day.

    After the impact refactor, DigestService.compute_impact delegates to the
    shared ``impact`` module, so the volume source is patched there.
    """
    svc = DigestService(db=MagicMock(), org_id="org-1")
    with patch(
        "app.services.perception.impact.get_avg_daily_volume",
        return_value=120.0,
    ) as m:
        impact = svc.compute_impact(_ALERT)
    m.assert_called_once()
    # |9900 × 16.82%| ÷ 7 × 120 tons/day
    expected = abs(9900.0 * 16.82 / 100) / 7 * 120.0
    assert impact["impact_yuan_per_day"] == expected
    assert impact["avg_daily_volume_tons"] == 120.0
