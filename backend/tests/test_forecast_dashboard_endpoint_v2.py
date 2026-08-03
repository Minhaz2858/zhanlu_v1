"""Tests for get_forecast_base_batch_v2 — reads forecasts from local
ForecastRun table instead of the stale EDIA v_forecast_price view."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.forecasting import ForecastTarget, ForecastRun
from app.services.market_dashboard_service import get_forecast_base_batch_v2


@pytest.fixture
def db_session():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng, tables=[
        ForecastTarget.__table__,
        ForecastRun.__table__,
    ])
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()


def _seed_target_and_run(db, product_key: str, base_7d: float, base_3d: float = None):
    """Insert a ForecastTarget + latest ForecastRun with the given 7d base."""
    if base_3d is None:
        base_3d = base_7d - 50
    target = ForecastTarget(
        org_id="default-org",
        app_id="default-app",
        product_key=product_key,
        name=product_key,
        datasource={"source": "edia_mysql"},
        level=2,
        status="active",
        source="seed",
    )
    db.add(target)
    db.flush()
    run = ForecastRun(
        org_id="default-org",
        app_id="default-app",
        target_id=target.id,
        results={
            "3d": {"base": [base_3d], "bull": [base_3d + 100], "bear": [base_3d - 100]},
            "7d": {"base": [base_7d], "bull": [base_7d + 100], "bear": [base_7d - 100]},
            "30d": {"base": [base_7d + 200], "bull": [base_7d + 300], "bear": [base_7d + 100]},
        },
        below_naive_baseline=False,
        confidence="Medium",
        as_of_date=datetime.now(timezone.utc),
        model_detail={"models_run": ["arima", "ets"]},
    )
    db.add(run)
    db.commit()


def test_v2_returns_base_7d_from_latest_forecast_run(db_session):
    _seed_target_and_run(db_session, "ecisco.crude_oil", 84.5)

    result = get_forecast_base_batch_v2(["crude_oil", "naphtha"], day=7, db=db_session)
    assert result.day == 7
    assert result.base_by_product["crude_oil"] == 84.5
    assert result.base_by_product["naphtha"] is None  # no run seeded


def test_v2_returns_none_for_product_with_no_run(db_session):
    result = get_forecast_base_batch_v2(["crude_oil"], day=7, db=db_session)
    assert result.base_by_product["crude_oil"] is None


def test_v2_respects_day_parameter(db_session):
    _seed_target_and_run(db_session, "ecisco.crude_oil", base_7d=84.5, base_3d=34.5)

    # Request 3-day forecast instead of 7-day
    result = get_forecast_base_batch_v2(["crude_oil"], day=3, db=db_session)
    assert result.day == 3
    assert result.base_by_product["crude_oil"] == 34.5


def test_v2_returns_latest_run_when_multiple_exist(db_session):
    target = ForecastTarget(
        org_id="default-org", app_id="default-app",
        product_key="ecisco.crude_oil", name="Crude",
        datasource={"source": "edia_mysql"}, level=2, status="active", source="seed",
    )
    db_session.add(target)
    db_session.flush()
    old_run = ForecastRun(
        org_id="default-org", app_id="default-app", target_id=target.id,
        results={"7d": {"base": [70.0], "bull": [75.0], "bear": [65.0]}},
        below_naive_baseline=False, confidence="Low",
    )
    db_session.add(old_run)
    db_session.commit()
    new_run = ForecastRun(
        org_id="default-org", app_id="default-app", target_id=target.id,
        results={"7d": {"base": [85.0], "bull": [90.0], "bear": [80.0]}},
        below_naive_baseline=False, confidence="Medium",
    )
    db_session.add(new_run)
    db_session.commit()

    result = get_forecast_base_batch_v2(["crude_oil"], day=7, db=db_session)
    # Should return the newer run's base (85.0), not the old one (70.0)
    assert result.base_by_product["crude_oil"] == 85.0
