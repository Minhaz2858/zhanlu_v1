"""Tests for Wave 3 T3.3 — ImportPriceLoader (reads from forecast_external_points)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.database import Base, SessionLocal, engine
from app.models.forecasting import (
    ForecastExternalPoint,
    ForecastExternalSeries,
)


@pytest.fixture(autouse=True)
def _schema_and_clean():
    # Mock the MySQL warehouse engine so warehouse fallback returns empty.
    # This isolates tests to PG-only behavior (the warehouse fallback is
    # tested separately in test_warehouse_loaders.py).
    from unittest.mock import patch

    with patch(
        "app.services.forecasting.features.warehouse_loaders._resolve_mysql_engine",
        return_value=None,
    ), patch(
        "app.services.forecasting.features.exogenous_loaders._resolve_mysql_engine",
        return_value=None,
    ):
        Base.metadata.create_all(engine)
        session = SessionLocal()
        try:
            try:
                session.query(ForecastExternalPoint).delete()
            except Exception:
                pass
            try:
                session.query(ForecastExternalSeries).delete()
            except Exception:
                pass
            session.commit()
        finally:
            session.close()
        yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _seed_series(db, domain: str, product_key="isoprene",
                 series_key=None) -> ForecastExternalSeries:
    s = ForecastExternalSeries(
        series_key=series_key or f"test_{domain}_{product_key}",
        domain=domain, product_key=product_key, source="csv_upload",
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _seed_points(db, series_id, points):
    for d, v in points:
        db.add(ForecastExternalPoint(series_id=series_id, date=d, value=v))
    db.commit()


class TestImportPriceLoader:
    """Wave 3 T3.3 — ImportPriceLoader (domain='import_price')."""

    def test_no_session_returns_empty(self):
        from app.services.forecasting.features.exogenous_loaders import (
            ImportPriceLoader,
        )
        loader = ImportPriceLoader(product_id="isoprene", db_session=None)
        df = loader.load()
        assert isinstance(df, pd.DataFrame)
        assert df.empty
        assert list(df.columns) == ["date", "import_price_cny"]

    def test_loads_data_for_matching_product(self, db):
        from app.services.forecasting.features.exogenous_loaders import (
            ImportPriceLoader,
        )
        s = _seed_series(db, "import_price", "isoprene")
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        points = [
            (today - timedelta(days=10), 12.50),
            (today - timedelta(days=5), 12.80),
        ]
        _seed_points(db, s.id, points)
        loader = ImportPriceLoader(product_id="isoprene", db_session=db)
        df = loader.load()
        assert len(df) == 2
        assert list(df.columns) == ["date", "import_price_cny"]

    def test_wrong_domain_returns_empty(self, db):
        from app.services.forecasting.features.exogenous_loaders import (
            ImportPriceLoader,
        )
        s = _seed_series(db, "inventory", "isoprene")
        _seed_points(db, s.id, [(datetime(2025, 1, 1), 5000.0)])
        loader = ImportPriceLoader(product_id="isoprene", db_session=db)
        df = loader.load()
        assert df.empty

    def test_lookback_filter(self, db):
        from app.services.forecasting.features.exogenous_loaders import (
            ImportPriceLoader,
        )
        s = _seed_series(db, "import_price", "isoprene")
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        points = [
            (today - timedelta(days=400), 12.0),
            (today - timedelta(days=100), 13.0),
        ]
        _seed_points(db, s.id, points)
        loader = ImportPriceLoader(
            product_id="isoprene", lookback_days=365, db_session=db,
        )
        df = loader.load()
        assert len(df) == 1
        assert float(df.iloc[0]["import_price_cny"]) == 13.0