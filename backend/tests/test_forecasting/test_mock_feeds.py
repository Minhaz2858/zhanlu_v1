"""Tests for Wave 3 T3.6 — mock_feeds generator + POST /seed-mock endpoint.

Since all 3 Tier-3 feeds are blocked, the mock generator synthesizes realistic
2-year weekly series so the entire pipeline (upload → store → load → signal →
feature → brief) can be tested end-to-end before real feeds arrive.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from app.database import Base, SessionLocal, engine
from app.models.forecasting import (
    ForecastExternalPoint,
    ForecastExternalSeries,
)
from app.services.forecasting.features.mock_feeds import (
    generate_mock_series,
    seed_mock_feeds,
    MOCK_OPERATING_RATE_RANGE,
    MOCK_INVENTORY_RANGE,
    MOCK_IMPORT_PRICE_RANGE,
)


@pytest.fixture(autouse=True)
def _schema_and_clean():
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
# generate_mock_series (pure function)
# ------------------------------------------------------------------ #

class TestGenerateMockSeries:

    def test_operating_rate_shape(self):
        df = generate_mock_series(
            domain="operating_rate", product_key="isoprene",
            n_weeks=52, end_date=datetime(2025, 1, 1),
        )
        assert list(df.columns) == ["date", "op_rate"]
        assert len(df) == 52
        # All values within the documented range
        assert df["op_rate"].min() >= MOCK_OPERATING_RATE_RANGE[0]
        assert df["op_rate"].max() <= MOCK_OPERATING_RATE_RANGE[1]

    def test_inventory_shape(self):
        df = generate_mock_series(
            domain="inventory", product_key="toluene",
            n_weeks=104,
        )
        assert list(df.columns) == ["date", "inventory_t"]
        assert len(df) == 104
        assert df["inventory_t"].min() >= MOCK_INVENTORY_RANGE[0]
        assert df["inventory_t"].max() <= MOCK_INVENTORY_RANGE[1]

    def test_import_price_shape(self):
        df = generate_mock_series(
            domain="import_price", product_key="isoprene",
            n_weeks=52,
        )
        assert list(df.columns) == ["date", "import_price_cny"]
        assert len(df) == 52
        assert df["import_price_cny"].min() >= MOCK_IMPORT_PRICE_RANGE[0]
        assert df["import_price_cny"].max() <= MOCK_IMPORT_PRICE_RANGE[1]

    def test_dates_sorted_ascending(self):
        df = generate_mock_series(
            domain="operating_rate", product_key="isoprene",
            n_weeks=52, end_date=datetime(2025, 6, 1),
        )
        dates = list(df["date"])
        assert dates == sorted(dates)
        # All dates strictly in the past relative to the end_date
        for d in dates:
            assert d <= datetime(2025, 6, 1)

    def test_unknown_domain_returns_empty(self):
        df = generate_mock_series(
            domain="weather", product_key="isoprene", n_weeks=10,
        )
        assert df.empty


# ------------------------------------------------------------------ #
# seed_mock_feeds (writes to DB)
# ------------------------------------------------------------------ #

class TestSeedMockFeeds:

    def test_seeds_all_three_domains(self, db):
        result = seed_mock_feeds(
            db=db,
            product_key="isoprene",
            n_weeks=52,
        )
        assert set(result.keys()) == {"operating_rate", "inventory", "import_price"}
        for domain, info in result.items():
            assert info["row_count"] == 52
            assert info["series_key"].startswith("mock_")

        # Verify all 3 series exist with source='mock'
        series = db.query(ForecastExternalSeries).filter_by(
            source="mock",
        ).all()
        assert len(series) == 3
        domains = {s.domain for s in series}
        assert domains == {"operating_rate", "inventory", "import_price"}

    def test_seed_is_idempotent(self, db):
        """Seeding twice with the same product_key re-seeds the same series."""
        result1 = seed_mock_feeds(db=db, product_key="isoprene", n_weeks=52)
        result2 = seed_mock_feeds(db=db, product_key="isoprene", n_weeks=52)
        # row_count should be 52 in both (replaced, not appended)
        for domain in result1:
            assert result1[domain]["row_count"] == result2[domain]["row_count"]

    def test_seeded_series_can_be_loaded(self, db):
        """End-to-end: seed → loader reads back the data."""
        from app.services.forecasting.features.exogenous_loaders import (
            OperatingRateLoader, InventoryLoader, ImportPriceLoader,
        )

        seed_mock_feeds(db=db, product_key="isoprene", n_weeks=52)

        op_df = OperatingRateLoader(product_id="isoprene", db_session=db).load()
        assert len(op_df) == 52
        assert list(op_df.columns) == ["date", "op_rate"]

        inv_df = InventoryLoader(product_id="isoprene", db_session=db).load()
        assert len(inv_df) == 52
        assert list(inv_df.columns) == ["date", "inventory_t"]

        ip_df = ImportPriceLoader(product_id="isoprene", db_session=db).load()
        assert len(ip_df) == 52
        assert list(ip_df.columns) == ["date", "import_price_cny"]

    def test_seeded_data_produces_valid_signals(self, db):
        """Mock data should produce valid (non-None) signals for signal modules."""
        from app.services.forecasting.features.operating_signal import (
            compute_operating_signal,
        )
        from app.services.forecasting.features.inventory_signal import (
            compute_inventory_signal,
        )
        from app.services.forecasting.features.import_parity_signal import (
            compute_import_parity_signal,
        )
        from app.services.forecasting.features.exogenous_loaders import (
            OperatingRateLoader, InventoryLoader, ImportPriceLoader,
        )

        seed_mock_feeds(db=db, product_key="isoprene", n_weeks=104)

        op_df = OperatingRateLoader(product_id="isoprene", db_session=db).load()
        inv_df = InventoryLoader(product_id="isoprene", db_session=db).load()
        ip_df = ImportPriceLoader(product_id="isoprene", db_session=db).load()

        op_sig = compute_operating_signal(op_df, product_id="isoprene")
        assert op_sig.has_sufficient_data is True
        assert op_sig.rolling_4wk_op_rate is not None
        assert op_sig.utilization_regime in ("tight", "normal", "loose")

        inv_sig = compute_inventory_signal(inv_df, product_id="isoprene")
        assert inv_sig.has_sufficient_data is True
        assert inv_sig.inventory_pressure in ("high", "normal", "low")

        ip_sig = compute_import_parity_signal(ip_df, product_id="isoprene")
        assert ip_sig.has_sufficient_data is True  # import-only mode

    def test_seeded_series_metadata_correct(self, db):
        seed_mock_feeds(db=db, product_key="isoprene", n_weeks=52)
        s = db.query(ForecastExternalSeries).filter_by(
            series_key="mock_op_rate_isoprene",
        ).first()
        assert s is not None
        assert s.domain == "operating_rate"
        assert s.source == "mock"
        assert s.product_key == "isoprene"
        assert s.row_count == 52
        assert s.last_value_date is not None