"""The domain-config-driven seeder must register the 11 canonical Ecisco BI
forecast targets defined in ``app/domain_configs/default.json``.

The targets were de-hardcoded out of a bespoke seed module into the per-app
domain config; ``seed_forecast_targets()`` reads them at runtime (an empty
agent name resolves the default config). See migration 037.
"""

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


def test_seed_creates_eleven_forecast_targets(in_memory_db):
    from app.services.forecasting.seed_targets import seed_forecast_targets
    n = seed_forecast_targets(in_memory_db, org_id="org-1", app_id="app-1")
    assert n == 11, f"Expected 11 targets, got {n}"


def test_seed_targets_have_datasource_with_required_fields(in_memory_db):
    from app.services.forecasting.seed_targets import seed_forecast_targets
    seed_forecast_targets(in_memory_db, org_id="org-1", app_id="app-1")
    targets = in_memory_db.query(ForecastTarget).all()
    assert len(targets) == 11, f"Expected 11 seeded targets, got {len(targets)}"
    for t in targets:
        ds = t.datasource
        assert "table" in ds, f"Target {t.product_key} missing 'table' in datasource"
        assert "time_col" in ds, f"Target {t.product_key} missing 'time_col'"
        assert "measure" in ds, f"Target {t.product_key} missing 'measure'"


def test_seed_is_idempotent(in_memory_db):
    from app.services.forecasting.seed_targets import seed_forecast_targets
    seed_forecast_targets(in_memory_db, org_id="org-1", app_id="app-1")
    seed_forecast_targets(in_memory_db, org_id="org-1", app_id="app-1")
    n = in_memory_db.query(ForecastTarget).count()
    assert n == 11, f"Seed should be idempotent; got {n} targets after 2 runs"
