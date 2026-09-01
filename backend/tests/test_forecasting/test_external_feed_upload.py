"""Tests for Wave 3 T3.0 CSV upload service (ingest/upsert into external-feed store).

The service ingests a user-uploaded CSV (`date,value` + optional metadata),
parses/validates rows, upserts into ``forecast_external_points``, and updates
the parent ``forecast_external_series`` roll-up stats.
"""
from __future__ import annotations

import io
from datetime import datetime

import pandas as pd
import pytest

from app.database import Base, SessionLocal, engine
from app.models.forecasting import (
    ForecastExternalPoint,
    ForecastExternalSeries,
)
from app.services.forecasting.features.external_feed_ingest import (
    IngestError,
    ingest_csv,
    list_series,
    get_series_points,
    delete_series,
)


# ------------------------------------------------------------------ #
# Schema bootstrap + per-test cleanup
# ------------------------------------------------------------------ #

@pytest.fixture(autouse=True)
def _schema_and_clean():
    """Ensure tables exist then truncate them before each test for isolation.

    The shared in-memory SQLite engine (see conftest.py) persists across the
    whole test session, so without cleanup rows leak between tests.
    """
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        # Delete points first (FK to series), then series
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
# Helpers
# ------------------------------------------------------------------ #

def _make_csv(text: str) -> io.BytesIO:
    """Convert a CSV string into an in-memory file-like object."""
    return io.BytesIO(text.encode("utf-8"))


# ------------------------------------------------------------------ #
# ingest_csv: happy path
# ------------------------------------------------------------------ #

class TestIngestCsvHappyPath:
    """Valid CSVs should populate the store and update roll-up stats."""

    def test_minimal_two_column_csv(self, db):
        csv = "date,value\n2025-01-01,70.5\n2025-01-08,72.0\n2025-01-15,75.5\n"
        result = ingest_csv(
            db=db,
            file=_make_csv(csv),
            series_key="op_rate_iso",
            domain="operating_rate",
            product_key="isoprene",
            unit="%",
            cadence="weekly",
            uploaded_by="test_user",
        )

        assert result["series_key"] == "op_rate_iso"
        assert result["domain"] == "operating_rate"
        assert result["product_key"] == "isoprene"
        assert result["row_count"] == 3
        assert result["rows_inserted"] == 3
        assert result["rows_updated"] == 0
        # Returned as ISO string for API consumers
        assert result["last_value_date"] == "2025-01-15T00:00:00"

    def test_csv_with_metadata_column(self, db):
        csv = (
            "date,value,region\n"
            "2025-01-01,100.0,East\n"
            "2025-01-02,105.0,East\n"
        )
        result = ingest_csv(
            db=db,
            file=_make_csv(csv),
            series_key="inv_iso",
            domain="inventory",
            product_key="isoprene",
            unit="吨",
        )
        assert result["row_count"] == 2

        p = db.query(ForecastExternalPoint).filter(
            ForecastExternalPoint.series_id.isnot(None),
        ).first()
        # Metadata should have been captured
        assert p.metadata_ is not None
        assert p.metadata_.get("region") in ("East", None)

    def test_upsert_replaces_existing_date(self, db):
        """Uploading the same date twice updates the value (not duplicates)."""
        csv_v1 = "date,value\n2025-01-01,70.0\n"
        ingest_csv(
            db=db, file=_make_csv(csv_v1),
            series_key="op_rate_iso", domain="operating_rate",
            product_key="isoprene",
        )
        csv_v2 = "date,value\n2025-01-01,80.0\n2025-01-08,82.0\n"
        result = ingest_csv(
            db=db, file=_make_csv(csv_v2),
            series_key="op_rate_iso", domain="operating_rate",
            product_key="isoprene",
        )
        assert result["rows_inserted"] == 1
        assert result["rows_updated"] == 1
        assert result["row_count"] == 2

        # Verify the value was actually overwritten
        s = db.query(ForecastExternalSeries).filter_by(
            series_key="op_rate_iso",
        ).first()
        p = db.query(ForecastExternalPoint).filter_by(
            series_id=s.id, date=datetime(2025, 1, 1),
        ).first()
        assert p.value == 80.0

    def test_creates_series_on_first_upload(self, db):
        """If series_key doesn't exist, ingest creates it."""
        assert db.query(ForecastExternalSeries).filter_by(
            series_key="new_feed",
        ).first() is None

        ingest_csv(
            db=db, file=_make_csv("date,value\n2025-01-01,1.0\n"),
            series_key="new_feed", domain="operating_rate",
            product_key="p1",
        )
        s = db.query(ForecastExternalSeries).filter_by(
            series_key="new_feed",
        ).first()
        assert s is not None
        assert s.source == "csv_upload"

    def test_product_key_optional(self, db):
        """Series may be product-agnostic (industry-wide inventory)."""
        result = ingest_csv(
            db=db, file=_make_csv("date,value\n2025-01-01,5000.0\n"),
            series_key="inv_total", domain="inventory",
            product_key=None,
        )
        assert result["row_count"] == 1
        s = db.query(ForecastExternalSeries).filter_by(
            series_key="inv_total",
        ).first()
        assert s.product_key is None


# ------------------------------------------------------------------ #
# ingest_csv: validation errors
# ------------------------------------------------------------------ #

class TestIngestCsvValidation:
    """Bad CSVs should raise IngestError with actionable messages."""

    def test_missing_date_column_raises(self, db):
        csv = "when,value\n2025-01-01,1.0\n"
        with pytest.raises(IngestError, match="date.*column"):
            ingest_csv(
                db=db, file=_make_csv(csv),
                series_key="x", domain="operating_rate",
                product_key="p1",
            )

    def test_missing_value_column_raises(self, db):
        csv = "date,val\n2025-01-01,1.0\n"
        with pytest.raises(IngestError, match="value.*column"):
            ingest_csv(
                db=db, file=_make_csv(csv),
                series_key="x", domain="operating_rate",
                product_key="p1",
            )

    def test_invalid_date_raises(self, db):
        csv = "date,value\nnot-a-date,1.0\n"
        with pytest.raises(IngestError, match="date"):
            ingest_csv(
                db=db, file=_make_csv(csv),
                series_key="x", domain="operating_rate",
                product_key="p1",
            )

    def test_non_numeric_value_raises(self, db):
        csv = "date,value\n2025-01-01,not-a-number\n"
        with pytest.raises(IngestError, match="value"):
            ingest_csv(
                db=db, file=_make_csv(csv),
                series_key="x", domain="operating_rate",
                product_key="p1",
            )

    def test_empty_csv_raises(self, db):
        with pytest.raises(IngestError, match="empty|no rows"):
            ingest_csv(
                db=db, file=_make_csv(""),
                series_key="x", domain="operating_rate",
                product_key="p1",
            )

    def test_only_header_raises(self, db):
        with pytest.raises(IngestError, match="empty|no rows"):
            ingest_csv(
                db=db, file=_make_csv("date,value\n"),
                series_key="x", domain="operating_rate",
                product_key="p1",
            )

    def test_invalid_domain_raises(self, db):
        csv = "date,value\n2025-01-01,1.0\n"
        with pytest.raises(IngestError, match="domain"):
            ingest_csv(
                db=db, file=_make_csv(csv),
                series_key="x", domain="not_a_real_domain",
                product_key="p1",
            )

    def test_blank_series_key_raises(self, db):
        csv = "date,value\n2025-01-01,1.0\n"
        with pytest.raises(IngestError, match="series_key"):
            ingest_csv(
                db=db, file=_make_csv(csv),
                series_key="", domain="operating_rate",
                product_key="p1",
            )


# ------------------------------------------------------------------ #
# CRUD: list_series / get_series_points / delete_series
# ------------------------------------------------------------------ #

class TestSeriesCrud:

    def test_list_series_returns_all_for_org(self, db):
        ingest_csv(
            db=db, file=_make_csv("date,value\n2025-01-01,1.0\n"),
            series_key="s1", domain="operating_rate", product_key="p1",
        )
        ingest_csv(
            db=db, file=_make_csv("date,value\n2025-01-01,2.0\n"),
            series_key="s2", domain="inventory", product_key="p2",
        )
        rows = list_series(db)
        assert len(rows) == 2
        keys = {r["series_key"] for r in rows}
        assert keys == {"s1", "s2"}

    def test_get_series_points_returns_dataframe_shape(self, db):
        ingest_csv(
            db=db,
            file=_make_csv("date,value\n2025-01-01,1.0\n2025-01-08,2.0\n"),
            series_key="s1", domain="operating_rate", product_key="p1",
        )
        df = get_series_points(db, "s1")
        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == ["date", "value"]
        assert len(df) == 2
        assert float(df.iloc[0]["value"]) == 1.0

    def test_get_series_points_unknown_returns_empty(self, db):
        df = get_series_points(db, "nonexistent")
        assert df.empty

    def test_delete_series_removes_points_cascade(self, db):
        ingest_csv(
            db=db,
            file=_make_csv("date,value\n2025-01-01,1.0\n2025-01-08,2.0\n"),
            series_key="doomed", domain="operating_rate", product_key="p1",
        )
        s = db.query(ForecastExternalSeries).filter_by(series_key="doomed").first()
        assert db.query(ForecastExternalPoint).filter_by(series_id=s.id).count() == 2

        delete_series(db, "doomed")

        assert db.query(ForecastExternalSeries).filter_by(series_key="doomed").first() is None
        assert db.query(ForecastExternalPoint).filter_by(series_id=s.id).count() == 0

    def test_delete_unknown_is_no_op(self, db):
        # Should not raise
        delete_series(db, "nonexistent")