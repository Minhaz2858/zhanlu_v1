"""Tests for Wave 3 T3.1 — OperatingRateLoader (reads from forecast_external_points)."""
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
        domain=domain,
        product_key=product_key,
        source="csv_upload",
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _seed_points(db, series_id, points):
    for d, v in points:
        db.add(ForecastExternalPoint(series_id=series_id, date=d, value=v))
    db.commit()


class TestOperatingRateLoader:
    """Wave 3 T3.1 — OperatingRateLoader (domain='operating_rate')."""

    def test_no_session_returns_empty(self):
        from app.services.forecasting.features.exogenous_loaders import (
            OperatingRateLoader,
        )
        loader = OperatingRateLoader(product_id="isoprene", db_session=None)
        df = loader.load()
        assert isinstance(df, pd.DataFrame)
        assert df.empty
        assert list(df.columns) == ["date", "op_rate"]

    def test_loads_data_for_matching_product(self, db):
        from app.services.forecasting.features.exogenous_loaders import (
            OperatingRateLoader,
        )
        s = _seed_series(db, "operating_rate", "isoprene")
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        points = [
            (today - timedelta(days=10), 75.0),
            (today - timedelta(days=5), 78.0),
            (today - timedelta(days=2), 80.0),
        ]
        _seed_points(db, s.id, points)

        loader = OperatingRateLoader(product_id="isoprene", db_session=db)
        df = loader.load()
        assert len(df) == 3
        assert list(df.columns) == ["date", "op_rate"]
        assert sorted(df["op_rate"].tolist()) == [75.0, 78.0, 80.0]

    def test_wrong_domain_returns_empty(self, db):
        from app.services.forecasting.features.exogenous_loaders import (
            OperatingRateLoader,
        )
        s = _seed_series(db, "inventory", "isoprene")
        _seed_points(db, s.id, [(datetime(2025, 1, 1), 100.0)])
        loader = OperatingRateLoader(product_id="isoprene", db_session=db)
        df = loader.load()
        assert df.empty

    def test_wrong_product_returns_empty(self, db):
        from app.services.forecasting.features.exogenous_loaders import (
            OperatingRateLoader,
        )
        s = _seed_series(db, "operating_rate", "butadiene")
        _seed_points(db, s.id, [(datetime(2025, 1, 1), 70.0)])
        loader = OperatingRateLoader(product_id="isoprene", db_session=db)
        df = loader.load()
        assert df.empty

    def test_lookback_filter_excludes_old(self, db):
        from app.services.forecasting.features.exogenous_loaders import (
            OperatingRateLoader,
        )
        s = _seed_series(db, "operating_rate", "isoprene")
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        points = [
            (today - timedelta(days=400), 70.0),  # too old for lookback=365
            (today - timedelta(days=100), 75.0),  # within lookback
        ]
        _seed_points(db, s.id, points)

        loader = OperatingRateLoader(
            product_id="isoprene", lookback_days=365, db_session=db,
        )
        df = loader.load()
        assert len(df) == 1
        assert float(df.iloc[0]["op_rate"]) == 75.0

    def test_future_dates_excluded(self, db):
        """Loader never returns rows with date > today (no future leakage)."""
        from app.services.forecasting.features.exogenous_loaders import (
            OperatingRateLoader,
        )
        s = _seed_series(db, "operating_rate", "isoprene")
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        points = [
            (today - timedelta(days=10), 75.0),    # past
            (today + timedelta(days=10), 80.0),    # future
            (today + timedelta(days=30), 85.0),    # future
        ]
        _seed_points(db, s.id, points)

        loader = OperatingRateLoader(product_id="isoprene", db_session=db)
        df = loader.load()
        assert len(df) == 1
        assert float(df.iloc[0]["op_rate"]) == 75.0

    def test_returns_empty_when_no_points(self, db):
        from app.services.forecasting.features.exogenous_loaders import (
            OperatingRateLoader,
        )
        _seed_series(db, "operating_rate", "isoprene")
        loader = OperatingRateLoader(product_id="isoprene", db_session=db)
        df = loader.load()
        assert df.empty

    def test_results_sorted_ascending(self, db):
        from app.services.forecasting.features.exogenous_loaders import (
            OperatingRateLoader,
        )
        s = _seed_series(db, "operating_rate", "isoprene")
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        points = [
            (today - timedelta(days=5), 80.0),
            (today - timedelta(days=20), 75.0),
            (today - timedelta(days=10), 78.0),
        ]
        _seed_points(db, s.id, points)

        loader = OperatingRateLoader(product_id="isoprene", db_session=db)
        df = loader.load()
        dates = list(df["date"])
        assert dates == sorted(dates)