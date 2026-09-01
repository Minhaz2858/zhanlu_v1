"""Tests for the ERP transaction loader (Phase F2)."""
from __future__ import annotations

import pytest
import pandas as pd
from datetime import date
from sqlalchemy import create_engine, text


@pytest.fixture
def sqlite_engine():
    """In-memory SQLite with one ERP table containing 2018-2026 transactions."""
    eng = create_engine("sqlite:///:memory:")
    with eng.connect() as conn:
        conn.execute(text(
            "CREATE TABLE `sale_erp_v_异戊二烯_data` ("
            "`Unnamed: 0` TEXT, `qty` REAL, `date` TEXT, "
            "`price` REAL, `amount` REAL, `product` TEXT, `supplier` TEXT)"
        ))
        # Insert: 3 days, multiple txns per day, plus a non-matching product
        rows = [
            ("r1", 100, "2018-07-01", 9300.0, 930000.0, "异戊二烯", "中石化华中"),
            ("r2", 50, "2018-07-01", 9310.0, 465500.0, "异戊二烯", "恒河材料"),  # same day
            ("r3", 200, "2018-07-02", 9350.0, 1870000.0, "异戊二烯", "中石化华中"),
            ("r4", 300, "2026-06-30", 12000.0, 3600000.0, "异戊二烯", "恒河材料"),
            ("r5", 100, "2026-07-01", 12050.0, 1205000.0, "异戊二烯", "恒河材料"),
            ("r6", 100, "2026-07-02", 9999.0, 999900.0, "苯乙烯", "中石化华中"),  # wrong product
        ]
        for r in rows:
            conn.execute(text(
                "INSERT INTO `sale_erp_v_异戊二烯_data` "
                "(`Unnamed: 0`, `qty`, `date`, `price`, `amount`, `product`, `supplier`) "
                "VALUES (:u, :q, :d, :p, :a, :prod, :sup)"
            ), {
                "u": r[0], "q": r[1], "d": r[2], "p": r[3],
                "a": r[4], "prod": r[5], "sup": r[6],
            })
        conn.commit()
    yield eng


def test_erp_loader_filters_by_product(sqlite_engine):
    """F2: filter by product column excludes other products."""
    from app.services.forecasting.features.exogenous_loaders import ErpTxLoader
    loader = ErpTxLoader(engine=sqlite_engine)
    df = loader.load(
        erp_table="sale_erp_v_异戊二烯_data",
        product_filter="异戊二烯",
        window_start=date(2018, 1, 1),
        window_end=date(2026, 12, 31),
    )
    # 5 isoprene txns (r6 excluded), 4 distinct days (r1+r2 same day)
    assert len(df) == 4, df
    assert list(df.columns) == ["erp_price"]
    assert df.index.is_monotonic_increasing


def test_erp_loader_daily_mean(sqlite_engine):
    """F2: multiple transactions on same day are averaged."""
    from app.services.forecasting.features.exogenous_loaders import ErpTxLoader
    loader = ErpTxLoader(engine=sqlite_engine)
    df = loader.load(
        erp_table="sale_erp_v_异戊二烯_data",
        product_filter="异戊二烯",
        window_start=date(2018, 1, 1),
        window_end=date(2026, 12, 31),
    )
    # 2018-07-01 had two txns: 9300 + 9310 = 18610 / 2 = 9305.0
    july1 = df.loc[pd.Timestamp("2018-07-01"), "erp_price"]
    assert july1 == 9305.0, f"Expected 9305.0 mean, got {july1}"


def test_erp_loader_window_filter(sqlite_engine):
    """F2: window_start/end filters out rows outside range."""
    from app.services.forecasting.features.exogenous_loaders import ErpTxLoader
    loader = ErpTxLoader(engine=sqlite_engine)
    df = loader.load(
        erp_table="sale_erp_v_异戊二烯_data",
        product_filter="异戊二烯",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 12, 31),
    )
    # Only 2 days in 2026: 06-30 + 07-01
    assert len(df) == 2


def test_erp_loader_empty_on_missing_table(sqlite_engine):
    """F2: missing table returns empty DataFrame (does not raise)."""
    from app.services.forecasting.features.exogenous_loaders import ErpTxLoader
    loader = ErpTxLoader(engine=sqlite_engine)
    df = loader.load(
        erp_table="nonexistent_table",
        product_filter="异戊二烯",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 12, 31),
    )
    assert df.empty


def test_erp_loader_no_engine_returns_empty():
    """F2: no engine = empty DataFrame (engine hook is best-effort)."""
    from app.services.forecasting.features.exogenous_loaders import ErpTxLoader
    loader = ErpTxLoader(engine=None)
    # We don't actually need a real engine because the property will try
    # to resolve via _resolve_mysql_engine; if that's None, returns empty.
    # Use a sentinel None engine directly via monkey-patch:
    loader._engine = None
    # Force the property to return None
    from app.services.forecasting.features import exogenous_loaders as el
    original = el._resolve_mysql_engine
    el._resolve_mysql_engine = lambda: None
    try:
        df = loader.load(
            erp_table="sale_erp_v_x_data",
            product_filter="x",
            window_start=date(2026, 1, 1),
            window_end=date(2026, 12, 31),
        )
        assert df.empty
    finally:
        el._resolve_mysql_engine = original


def test_erp_loader_no_product_filter_takes_all(sqlite_engine):
    """F2: empty product_filter returns all rows (any product)."""
    from app.services.forecasting.features.exogenous_loaders import ErpTxLoader
    loader = ErpTxLoader(engine=sqlite_engine)
    df = loader.load(
        erp_table="sale_erp_v_异戊二烯_data",
        product_filter="",  # no filter
        window_start=date(2018, 1, 1),
        window_end=date(2026, 12, 31),
    )
    # All 6 rows, but r6 (苯乙烯) is on its own day, so 5 distinct days
    assert len(df) == 5