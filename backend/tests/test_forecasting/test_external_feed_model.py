"""Tests for Wave 3 external-feed ORM models (T3.0).

Two new tables:
    forecast_external_series  — registry of onboarded time-series feeds
    forecast_external_points  — individual data points per series

Both inherit TimestampedBase (UUID PK + org_id/app_id tenant wall).
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.database import Base, SessionLocal, engine
from app.models.forecasting import (
    ForecastExternalSeries,
    ForecastExternalPoint,
)


# ------------------------------------------------------------------ #
# Schema bootstrap — SQLite in-memory tests need all tables created
# ------------------------------------------------------------------ #

@pytest.fixture(autouse=True)
def _schema_and_clean():
    """Ensure tables exist then truncate them before each test for isolation."""
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


# ------------------------------------------------------------------ #
# Series model
# ------------------------------------------------------------------ #

class TestForecastExternalSeries:
    """Registry table — one row per onboarded feed."""

    def test_create_minimal_series(self, db):
        s = ForecastExternalSeries(
            series_key="op_rate_isoprene",
            domain="operating_rate",
            product_key="isoprene",
            unit="%",
            source="csv_upload",
            cadence="weekly",
        )
        db.add(s)
        db.commit()
        db.refresh(s)

        assert s.id is not None
        assert len(s.id) == 36  # UUID
        assert s.series_key == "op_rate_isoprene"
        assert s.domain == "operating_rate"
        assert s.product_key == "isoprene"
        assert s.unit == "%"
        assert s.source == "csv_upload"
        assert s.cadence == "weekly"
        assert s.row_count == 0
        assert s.created_date is not None

    def test_unique_series_key_per_org(self, db):
        """Same series_key within one org raises IntegrityError."""
        s1 = ForecastExternalSeries(
            series_key="inv_dl_toluene", domain="inventory",
            product_key="toluene", source="csv_upload",
        )
        db.add(s1)
        db.commit()

        s2 = ForecastExternalSeries(
            series_key="inv_dl_toluene", domain="inventory",
            product_key="toluene", source="csv_upload",
        )
        db.add(s2)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_domain_is_free_string_not_enum(self, db):
        """Domain is a free string column (operating_rate|inventory|import_price).

        We don't enforce enum at DB level so new domains can be added without
        schema migration — Wave 3 expansion path.
        """
        s = ForecastExternalSeries(
            series_key="custom_feed_1", domain="weather_forecast",
            source="api", product_key=None,
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        assert s.domain == "weather_forecast"

    def test_product_key_nullable(self, db):
        """Series can be product-agnostic (e.g. industry-wide inventory)."""
        s = ForecastExternalSeries(
            series_key="inv_industry_total", domain="inventory",
            product_key=None, source="csv_upload",
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        assert s.product_key is None

    def test_to_dict_round_trip(self, db):
        s = ForecastExternalSeries(
            series_key="import_cfr_isoprene", domain="import_price",
            product_key="isoprene", unit="CNY/kg", source="csv_upload",
            cadence="daily", row_count=42,
        )
        db.add(s)
        db.commit()
        d = s.to_dict()
        assert d["series_key"] == "import_cfr_isoprene"
        assert d["domain"] == "import_price"
        assert d["row_count"] == 42
        assert d["unit"] == "CNY/kg"


# ------------------------------------------------------------------ #
# Point model
# ------------------------------------------------------------------ #

class TestForecastExternalPoint:
    """Per-series data points."""

    def test_create_point(self, db):
        s = ForecastExternalSeries(
            series_key="op_rate_iso", domain="operating_rate",
            product_key="isoprene", source="csv_upload",
        )
        db.add(s)
        db.commit()

        p = ForecastExternalPoint(
            series_id=s.id, date=datetime(2025, 1, 1), value=75.5,
        )
        db.add(p)
        db.commit()
        db.refresh(p)

        assert p.id is not None
        assert p.series_id == s.id
        assert p.date == datetime(2025, 1, 1)
        assert p.value == 75.5

    def test_unique_point_per_series_date(self, db):
        """Two points with same (series_id, date) violate unique constraint."""
        s = ForecastExternalSeries(
            series_key="op_rate_iso2", domain="operating_rate",
            product_key="isoprene", source="csv_upload",
        )
        db.add(s)
        db.commit()

        p1 = ForecastExternalPoint(
            series_id=s.id, date=datetime(2025, 1, 1), value=70.0,
        )
        db.add(p1)
        db.commit()

        p2 = ForecastExternalPoint(
            series_id=s.id, date=datetime(2025, 1, 1), value=80.0,
        )
        db.add(p2)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_metadata_optional(self, db):
        s = ForecastExternalSeries(
            series_key="inv_dl_iso", domain="inventory",
            product_key="isoprene", source="csv_upload",
        )
        db.add(s)
        db.commit()

        p = ForecastExternalPoint(
            series_id=s.id, date=datetime(2025, 1, 1), value=100.0,
            metadata_={"region": "East China", "tank": "T-12"},
        )
        db.add(p)
        db.commit()
        db.refresh(p)
        assert p.metadata_ == {"region": "East China", "tank": "T-12"}

    def test_query_points_by_series(self, db):
        """Loader queries by series_id + date range; verify the shape."""
        s = ForecastExternalSeries(
            series_key="op_rate_iso3", domain="operating_rate",
            product_key="isoprene", source="csv_upload",
        )
        db.add(s)
        db.commit()

        for d, v in [(datetime(2025, 1, 1), 70.0),
                     (datetime(2025, 1, 8), 72.0),
                     (datetime(2025, 1, 15), 75.0)]:
            db.add(ForecastExternalPoint(series_id=s.id, date=d, value=v))
        db.commit()

        points = db.query(ForecastExternalPoint).filter(
            ForecastExternalPoint.series_id == s.id,
            ForecastExternalPoint.date >= datetime(2025, 1, 1),
            ForecastExternalPoint.date <= datetime(2025, 1, 15),
        ).order_by(ForecastExternalPoint.date).all()

        assert len(points) == 3
        assert [p.value for p in points] == [70.0, 72.0, 75.0]