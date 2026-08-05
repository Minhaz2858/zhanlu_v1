"""Tests for seed_ecisco_forecast_targets — idempotent seeder for the 11
Ecisco BI dashboard md_t_lz_price products (ERP products discovered dynamically)."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.forecasting import ForecastTarget
from app.services.forecasting.seed_ecisco_targets import (
    ECISCO_FORECAST_TARGETS,
    seed_ecisco_forecast_targets,
)


@pytest.fixture
def db_session():
    """In-memory SQLite session with forecasting tables created."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng, tables=[
        ForecastTarget.__table__,
    ])
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()


def test_ecisco_forecast_targets_has_11_md_lz_entries():
    """Static list now has 11 md_t_lz_price products. ERP products are
    discovered dynamically by discover_and_seed_sku_targets()."""
    assert len(ECISCO_FORECAST_TARGETS) == 11


def test_each_target_has_required_fields():
    for t in ECISCO_FORECAST_TARGETS:
        assert "product_key" in t
        assert t["product_key"].startswith("ecisco.")
        assert "name" in t
        assert "datasource" in t
        assert t["datasource"]["source"] == "edia_mysql"
        assert "table" in t["datasource"]
        assert "time_col" in t["datasource"]
        assert "measure" in t["datasource"]
        assert "where" in t["datasource"]
        assert "granularity" in t["datasource"]
        assert "include_in_weekly_report" in t
        assert "report_order" in t


def test_seeder_inserts_11_rows_on_first_call(db_session):
    count = seed_ecisco_forecast_targets(db_session, org_id="test-org", app_id="test-app")
    assert count == 11
    rows = db_session.query(ForecastTarget).filter(
        ForecastTarget.org_id == "test-org"
    ).all()
    assert len(rows) == 11
    # Spot-check one
    crude = db_session.query(ForecastTarget).filter(
        ForecastTarget.product_key == "ecisco.crude_oil"
    ).one()
    assert crude.status == "active"
    assert crude.source == "seed"
    assert crude.level == 2
    assert crude.include_in_weekly_report is True


def test_seeder_is_idempotent_second_call_inserts_zero(db_session):
    first = seed_ecisco_forecast_targets(db_session, org_id="test-org", app_id="test-app")
    assert first == 11
    second = seed_ecisco_forecast_targets(db_session, org_id="test-org", app_id="test-app")
    assert second == 0
    rows = db_session.query(ForecastTarget).filter(
        ForecastTarget.org_id == "test-org"
    ).all()
    assert len(rows) == 11  # still 11, not 22


def test_seeder_scoped_to_org_id(db_session):
    """Seeding for org-a should not affect org-b."""
    seed_ecisco_forecast_targets(db_session, org_id="org-a", app_id="app-a")
    seed_ecisco_forecast_targets(db_session, org_id="org-b", app_id="app-b")
    a_rows = db_session.query(ForecastTarget).filter(
        ForecastTarget.org_id == "org-a"
    ).all()
    b_rows = db_session.query(ForecastTarget).filter(
        ForecastTarget.org_id == "org-b"
    ).all()
    assert len(a_rows) == 11
    assert len(b_rows) == 11


def test_product_keys_are_unique():
    keys = [t["product_key"] for t in ECISCO_FORECAST_TARGETS]
    assert len(keys) == len(set(keys)), "Duplicate product_keys found"
