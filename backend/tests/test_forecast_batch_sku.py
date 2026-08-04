"""Tests for get_forecast_base_batch_v2 SKU lookup and drill-down."""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.forecasting import ForecastTarget, ForecastRun
from app.services.market_dashboard_service import get_forecast_base_batch_v2


@pytest.fixture
def db_session():
    """In-memory SQLite session with forecasting tables."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng, tables=[
        ForecastTarget.__table__,
        ForecastRun.__table__,
    ])
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()


def _make_sku_target(db, org_id, family, code, report_order, with_run=False, base_price=5000.0):
    """Insert a SKU ForecastTarget (+ optional ForecastRun) for testing."""
    target = ForecastTarget(
        org_id=org_id,
        app_id="test-app",
        product_key=f"ecisco.{family}.{code}",
        name=f"{family} SKU {code}",
        datasource={
            "source": "edia_mysql",
            "table": f"sale_erp_v_{family}_data",
            "time_col": "PLANDATE",
            "measure": "FTAXPRICE",
            "where": f"material_code = '{code}' AND FTAXPRICE > 0",
            "granularity": "day",
        },
        level=2,
        quality_grade="B",
        status="active",
        source="seed_sku",
        include_in_weekly_report=(report_order == 12),
        report_order=report_order,
    )
    db.add(target)
    db.commit()
    db.refresh(target)

    if with_run:
        run = ForecastRun(
            org_id=org_id,
            target_id=target.id,
            as_of_date=datetime(2026, 8, 4),
        results={
            "7d": {
                "base": [base_price],
                "bull": [base_price * 1.05],
                "bear": [base_price * 0.95],
            },
        },
        confidence="0.8",
            below_naive_baseline=False,
            model_detail={"metric": "ensemble"},
        )
        db.add(run)
        db.commit()
    return target


def test_sku_forecasts_returned_for_erp_product(db_session):
    """get_forecast_base_batch_v2 should return sku_forecasts_by_product
    for c5_resin with all SKU targets."""
    _make_sku_target(db_session, "default-org", "c5_resin", "PRIMARY", 12, with_run=True, base_price=5000.0)
    _make_sku_target(db_session, "default-org", "c5_resin", "SECONDARY", 13, with_run=True, base_price=6000.0)

    resp = get_forecast_base_batch_v2(["c5_resin"], day=7, db=db_session)
    assert "c5_resin" in resp.sku_forecasts_by_product
    skus = resp.sku_forecasts_by_product["c5_resin"]
    assert len(skus) == 2
    # Primary SKU (report_order=12) should be first
    assert skus[0].material_code == "PRIMARY"
    assert skus[0].is_primary is True
    assert skus[0].forecast_base == 5000.0
    assert skus[1].material_code == "SECONDARY"
    assert skus[1].is_primary is False
    assert skus[1].forecast_base == 6000.0


def test_primary_sku_base_used_for_main_forecast(db_session):
    """The main base_by_product[c5_resin] should use the primary SKU's forecast."""
    _make_sku_target(db_session, "default-org", "c5_resin", "PRIMARY", 12, with_run=True, base_price=5000.0)
    _make_sku_target(db_session, "default-org", "c5_resin", "SECONDARY", 13, with_run=True, base_price=6000.0)

    resp = get_forecast_base_batch_v2(["c5_resin"], day=7, db=db_session)
    assert resp.base_by_product["c5_resin"] == 5000.0


def test_no_sku_forecasts_for_md_lz_products(db_session):
    """md_t_lz_price products (e.g. crude_oil) should NOT have SKU data."""
    target = ForecastTarget(
        org_id="default-org",
        app_id="test-app",
        product_key="ecisco.crude_oil",
        name="Crude Oil (Brent)",
        datasource={
            "source": "edia_mysql",
            "table": "md_t_lz_price",
            "time_col": "FDATE",
            "measure": "FTAXPRICE",
            "where": "FMATERIAL_NAME = '裂解C5-布伦特'",
            "granularity": "day",
        },
        level=2,
        quality_grade="B",
        status="active",
        source="seed",
        include_in_weekly_report=True,
        report_order=1,
    )
    db_session.add(target)
    db_session.commit()

    run = ForecastRun(
        org_id="default-org",
        target_id=target.id,
        as_of_date=datetime(2026, 8, 4),
        results={"7d": {"base": [75.0]}},
        confidence="0.9",
        below_naive_baseline=False,
        model_detail={"metric": "ensemble"},
    )
    db_session.add(run)
    db_session.commit()

    resp = get_forecast_base_batch_v2(["crude_oil"], day=7, db=db_session)
    assert resp.base_by_product["crude_oil"] == 75.0
    # crude_oil should NOT appear in sku_forecasts_by_product
    assert "crude_oil" not in resp.sku_forecasts_by_product


def test_sku_forecasts_empty_when_no_targets(db_session):
    """No SKU targets → empty list (not missing key)."""
    resp = get_forecast_base_batch_v2(["c5_resin"], day=7, db=db_session)
    assert resp.base_by_product["c5_resin"] is None
    # Should return empty list, not None
    assert resp.sku_forecasts_by_product.get("c5_resin", []) == []


def test_both_erp_products_return_sku_data(db_session):
    """Both c5_resin and raffinate_c5 should return SKU lists."""
    _make_sku_target(db_session, "default-org", "c5_resin", "R1", 12, with_run=True, base_price=5000.0)
    _make_sku_target(db_session, "default-org", "raffinate_c5", "RC1", 12, with_run=True, base_price=4000.0)

    resp = get_forecast_base_batch_v2(["c5_resin", "raffinate_c5"], day=7, db=db_session)
    assert len(resp.sku_forecasts_by_product["c5_resin"]) == 1
    assert len(resp.sku_forecasts_by_product["raffinate_c5"]) == 1
    assert resp.base_by_product["c5_resin"] == 5000.0
    assert resp.base_by_product["raffinate_c5"] == 4000.0
