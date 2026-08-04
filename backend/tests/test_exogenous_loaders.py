"""Tests for exogenous feature loaders."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd

from app.services.forecasting.features.exogenous_loaders import (
    FeedstockLoader,
    FxLoader,
    EventFlagLoader,
)


# ---------------------------------------------------------------------------
# FxLoader tests
# ---------------------------------------------------------------------------

class TestFxLoader:
    def test_returns_static_rate(self):
        loader = FxLoader()
        df = loader.load(date(2026, 7, 1), date(2026, 7, 30))
        assert "usdcny" in df.columns
        assert len(df) == 30
        assert (df["usdcny"] == 7.10).all()

    def test_date_range_is_expected(self):
        loader = FxLoader()
        df = loader.load(date(2026, 8, 1), date(2026, 8, 3))
        assert len(df) == 3


# ---------------------------------------------------------------------------
# FeedstockLoader tests
# ---------------------------------------------------------------------------

class TestFeedstockLoader:
    def test_empty_keys_returns_empty_df(self):
        loader = FeedstockLoader(engine=None)
        df = loader.load([], date(2026, 7, 1), date(2026, 7, 30))
        assert df.empty

    def test_no_engine_returns_empty_df(self):
        loader = FeedstockLoader(engine=None)
        df = loader.load(["naphtha"], date(2026, 7, 1), date(2026, 7, 30))
        assert df.empty

    @patch("app.services.forecasting.features.exogenous_loaders._resolve_mysql_engine")
    def test_load_returns_price_columns(self, mock_resolve):
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.connect.return_value.__enter__.return_value = mock_conn
        mock_resolve.return_value = mock_engine

        rows = [
            ("2026-07-01", "5100"),
            ("2026-07-02", "5120"),
            ("2026-07-03", "5130"),
        ]
        mock_conn.execute.return_value.fetchall.return_value = rows

        loader = FeedstockLoader(engine=mock_engine)
        df = loader.load(["naphtha"], date(2026, 7, 1), date(2026, 7, 3))
        assert "naphtha" in df.columns
        assert len(df) == 3


# ---------------------------------------------------------------------------
# EventFlagLoader tests
# ---------------------------------------------------------------------------

class TestEventFlagLoader:
    def test_no_db_returns_zero_df(self):
        loader = EventFlagLoader(db_session=None)
        df = loader.load(date(2026, 7, 1), date(2026, 7, 30))
        assert len(df.columns) == len(EventFlagLoader.EVENT_TYPES_OF_INTEREST)
        assert (df.values == 0).all()

    def test_with_db_and_no_events_returns_zero(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []
        loader = EventFlagLoader(db_session=mock_db)
        df = loader.load(date(2026, 7, 1), date(2026, 7, 30))
        assert (df.values == 0).all()

    def test_event_sets_flag_within_lookback(self):
        """Event at Jul 5 with 30-day lookback flags all dates Jul 5 - Aug 4."""
        from datetime import datetime

        mock_db = MagicMock()
        mock_event = MagicMock()
        mock_event.created_date = datetime(2026, 7, 5)
        mock_event.event_type = "supply_disruption"
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_event]

        loader = EventFlagLoader(db_session=mock_db, lookback_days=30)
        df = loader.load(date(2026, 7, 1), date(2026, 7, 31))

        # Event at Jul 5: dates from Jul 5+ should be flagged
        assert df.loc[pd.Timestamp("2026-07-10"), "supply_disruption"] == 1
        # Jul 1 is before the event
        assert df.loc[pd.Timestamp("2026-07-01"), "supply_disruption"] == 0
