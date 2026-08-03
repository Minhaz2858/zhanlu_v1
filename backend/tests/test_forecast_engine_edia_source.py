"""Tests that ForecastEngine._fetch_series routes to EdiaMysqlDataSource
when target.datasource.source == 'edia_mysql'."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from app.services.forecasting.engine import ForecastEngine


def _make_target_with_edia_source():
    """Build a mock ForecastTarget with an edia_mysql datasource descriptor."""
    target = MagicMock()
    target.id = "test-target-1"
    target.org_id = "default-org"
    target.app_id = "default-app"
    target.datasource = {
        "source": "edia_mysql",
        "table": "md_t_lz_price",
        "time_col": "FDATE",
        "measure": "FTAXPRICE",
        "where": "FMATERIAL_NAME = '裂解C5-裂解C5均价'",
        "granularity": "day",
    }
    target.model_config = {}
    return target


def test_fetch_series_uses_edia_mysql_source_when_configured():
    """When datasource.source == 'edia_mysql', _fetch_series should delegate
    to EdiaMysqlDataSource.read_history and return a pandas Series."""
    target = _make_target_with_edia_source()
    fake_df = pd.DataFrame({
        "ds": pd.to_datetime(["2026-07-01", "2026-07-02", "2026-07-03"]),
        "y": [5500.0, 5600.0, 5700.0],
    })

    with patch(
        "app.services.forecasting.engine.EdiaMysqlDataSource"
    ) as MockSrc:
        MockSrc.return_value.read_history.return_value = fake_df
        engine = ForecastEngine(db=MagicMock())
        result = engine._fetch_series(target)

    # EdiaMysqlDataSource was instantiated and read_history was called
    MockSrc.return_value.read_history.assert_called_once_with(target.datasource)
    # Result is a pandas Series indexed by datetime
    assert isinstance(result, pd.Series)
    assert len(result) == 3
    assert result.iloc[0] == 5500.0


def test_fetch_series_returns_none_when_edia_source_raises():
    """If EdiaMysqlDataSource.read_history raises, _fetch_series should
    catch it, log a warning, and return None (so compute_target skips)."""
    target = _make_target_with_edia_source()

    with patch(
        "app.services.forecasting.engine.EdiaMysqlDataSource"
    ) as MockSrc:
        MockSrc.return_value.read_history.side_effect = RuntimeError(
            "EDIA MySQL unreachable"
        )
        engine = ForecastEngine(db=MagicMock())
        result = engine._fetch_series(target)

    assert result is None


def test_fetch_series_falls_through_to_kb_path_when_source_not_edia():
    """When datasource has no 'source' key (or it's not 'edia_mysql'),
    the existing KB-backed path should run unchanged. We verify by
    confirming EdiaMysqlDataSource is NOT instantiated."""
    target = MagicMock()
    target.datasource = {"table": "sales", "time_column": "date", "measure": "price"}
    target.model_config = {}
    target.org_id = "default-org"
    target.id = "test-target-2"

    with patch(
        "app.services.forecasting.engine.EdiaMysqlDataSource"
    ) as MockSrc:
        engine = ForecastEngine(db=MagicMock())
        # The KB path will fail because the mock db has no QueryService,
        # but we only care that EdiaMysqlDataSource was NOT called.
        try:
            engine._fetch_series(target)
        except Exception:
            pass  # expected — KB path fails on mock db

    MockSrc.assert_not_called()
