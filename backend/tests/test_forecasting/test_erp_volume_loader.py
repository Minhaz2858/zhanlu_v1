"""Tests for ErpVolumeLoader (Phase F1)."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest
from sqlalchemy import create_engine, text


@pytest.fixture
def sqlite_engine():
    """In-memory SQLite simulating sale_erp_v_isoprene_data with qty column."""
    eng = create_engine("sqlite:///:memory:")
    with eng.connect() as conn:
        conn.execute(text(
            "CREATE TABLE `sale_erp_v_isoprene_data` ("
            "`Unnamed: 0` TEXT, `qty` REAL, `date` TEXT, "
            "`price` REAL, `amount` REAL, `product` TEXT, `supplier` TEXT)"
        ))
        rows = [
            ("r1", 100.0, "2025-07-01", 9300.0, 930000.0, "\u5f02\u620a\u4e8c\u70ef", "\u4e2d\u77f3\u5316\u534e\u4e2d"),
            ("r2", 50.0, "2025-07-01", 9310.0, 465500.0, "\u5f02\u620a\u4e8c\u70ef", "\u6052\u6cb3\u6750\u6599"),
            ("r3", 200.0, "2025-07-02", 9350.0, 1870000.0, "\u5f02\u620a\u4e8c\u70ef", "\u4e2d\u77f3\u5316\u534e\u4e2d"),
            ("r4", 75.0, "2025-07-02", 9400.0, 705000.0, "\u5f02\u620a\u4e8c\u70ef", "\u6052\u6cb3\u6750\u6599"),
            ("r5", 300.0, "2025-07-03", 12000.0, 3600000.0, "\u5f02\u620a\u4e8c\u70ef", "\u6052\u6cb3\u6750\u6599"),
        ]
        for r in rows:
            conn.execute(text(
                "INSERT INTO `sale_erp_v_isoprene_data` "
                "(`Unnamed: 0`, `qty`, `date`, `price`, `amount`, `product`, `supplier`) "
                "VALUES (:u, :q, :d, :p, :a, :prod, :sup)"
            ), {
                "u": r[0], "q": r[1], "d": r[2], "p": r[3],
                "a": r[4], "prod": r[5], "sup": r[6],
            })
        conn.commit()
    yield eng


class TestErpVolumeLoader:
    def test_daily_sum_aggregation(self, sqlite_engine):
        """Multiple rows per day -> single row with summed volume."""
        from app.services.forecasting.features.exogenous_loaders import ErpVolumeLoader

        # Use 730-day lookback to ensure 2025 test data is within range
        loader = ErpVolumeLoader(product_id="isoprene", lookback_days=730)
        loader._engine = sqlite_engine

        result = loader.load()

        assert list(result.columns) == ["date", "volume"]
        assert len(result) == 3
        assert (
            result.loc[result["date"] == pd.Timestamp("2025-07-01"), "volume"].values[0]
            == 150.0
        )
        assert (
            result.loc[result["date"] == pd.Timestamp("2025-07-02"), "volume"].values[0]
            == 275.0
        )

    def test_empty_result(self):
        """Empty result set -> empty DataFrame, no crash."""
        from app.services.forecasting.features.exogenous_loaders import ErpVolumeLoader

        mock_connect = MagicMock()
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_connect.__enter__.return_value = mock_conn
        mock_connect.__exit__.return_value = None
        mock_pool = MagicMock()
        mock_pool.connect.return_value = mock_connect

        loader = ErpVolumeLoader(product_id="isoprene", lookback_days=90)
        loader._engine = mock_pool

        result = loader.load()

        assert list(result.columns) == ["date", "volume"]
        assert len(result) == 0

    def test_source_label(self):
        """source_label must be erp_volume."""
        from app.services.forecasting.features.exogenous_loaders import ErpVolumeLoader

        loader = ErpVolumeLoader(product_id="isoprene")
        assert loader.source_label == "erp_volume"

    def test_lookback_filter(self, sqlite_engine):
        """lookback_days restricts how far back data is read."""
        from app.services.forecasting.features.exogenous_loaders import ErpVolumeLoader

        loader = ErpVolumeLoader(product_id="isoprene", lookback_days=1)
        loader._engine = sqlite_engine

        result = loader.load()
        assert len(result) == 0

    def test_no_engine_returns_empty(self):
        """No engine -> empty DataFrame with correct columns."""
        from app.services.forecasting.features.exogenous_loaders import ErpVolumeLoader
        from app.services.forecasting.features import exogenous_loaders as el

        loader = ErpVolumeLoader(product_id="isoprene")
        loader._engine = None
        original = el._resolve_mysql_engine
        el._resolve_mysql_engine = lambda: None
        try:
            result = loader.load()
            assert list(result.columns) == ["date", "volume"]
            assert len(result) == 0
        finally:
            el._resolve_mysql_engine = original

    def test_missing_table_returns_empty(self, sqlite_engine):
        """Non-existent table -> empty DataFrame (no crash)."""
        from app.services.forecasting.features.exogenous_loaders import ErpVolumeLoader

        loader = ErpVolumeLoader(product_id="nonexistent_product", lookback_days=90)
        loader._engine = sqlite_engine

        result = loader.load()
        assert list(result.columns) == ["date", "volume"]
        assert len(result) == 0
