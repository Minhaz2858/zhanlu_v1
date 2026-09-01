"""Seed script must register 9 ForecastTargets for products with LZ view data."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.forecasting import ForecastTarget


@pytest.fixture
def in_memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_seed_creates_nine_forecast_targets(in_memory_db):
    from app.services.forecasting.seed_targets import seed_forecast_targets
    n = seed_forecast_targets(in_memory_db, org_id="org-1", app_id="app-1")
    assert n == 9, f"Expected 9 targets, got {n}"


def test_seed_targets_have_datasource_with_required_fields(in_memory_db):
    from app.services.forecasting.seed_targets import seed_forecast_targets
    seed_forecast_targets(in_memory_db, org_id="org-1", app_id="app-1")
    targets = in_memory_db.query(ForecastTarget).all()
    for t in targets:
        ds = t.datasource
        assert "table" in ds, f"Target {t.product_key} missing 'table' in datasource"
        assert "time_column" in ds, f"Target {t.product_key} missing 'time_column'"
        assert "measure" in ds, f"Target {t.product_key} missing 'measure'"
        assert "dimensions" in ds, f"Target {t.product_key} missing 'dimensions'"
        assert isinstance(ds["dimensions"], list), "dimensions must be a list"
        assert "kb_id" in ds, f"Target {t.product_key} missing 'kb_id' (engine cannot find KB)"


def test_seed_is_idempotent(in_memory_db):
    from app.services.forecasting.seed_targets import seed_forecast_targets
    seed_forecast_targets(in_memory_db, org_id="org-1", app_id="app-1")
    seed_forecast_targets(in_memory_db, org_id="org-1", app_id="app-1")
    n = in_memory_db.query(ForecastTarget).count()
    assert n == 9, f"Seed should be idempotent; got {n} targets after 2 runs"
