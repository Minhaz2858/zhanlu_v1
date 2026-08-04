"""Tests for the multi-source EdiaMysqlDataSource (Phase F1 Task F1)."""
from __future__ import annotations

import pytest
import pandas as pd
from sqlalchemy import create_engine, text


@pytest.fixture
def sqlite_engine():
    """In-memory SQLite with two tables: primary + extra."""
    eng = create_engine("sqlite:///:memory:")
    with eng.connect() as conn:
        conn.execute(text(
            "CREATE TABLE md_t_lz_price (FDATE TEXT, FTAXPRICE TEXT, "
            "FMATERIAL_NAME TEXT)"
        ))
        # Primary: 5 rows from 2026-07-01..05
        for i, d in enumerate(["2026-07-01", "2026-07-02", "2026-07-03",
                                "2026-07-04", "2026-07-05"]):
            conn.execute(text(
                "INSERT INTO md_t_lz_price VALUES (:d, :p, :m)"
            ), {"d": d, "p": str(1000 + i), "m": "isoprene-east"})
        # Extra: 10 rows from 2025-03-01..03 + 2026-07-02..03 (overlap)
        # Use the EDIA view column names with parens (sanity check)
        conn.execute(text(
            "CREATE TABLE `lz_v_异戊二烯_data` ("
            "`biz_date(业务日期)` TEXT, `tax_price(含税单价)` TEXT, "
            "`material_name(产品名称)` TEXT)"
        ))
        extras = [
            ("2025-03-01", "800", "异戊二烯"),
            ("2025-03-02", "810", "异戊二烯"),
            ("2025-03-03", "820", "异戊二烯"),
            ("2026-07-02", "999", "异戊二烯"),  # overlap, primary wins
            ("2026-07-03", "888", "异戊二烯"),
        ]
        for d, p, m in extras:
            conn.execute(text(
                "INSERT INTO `lz_v_异戊二烯_data` VALUES (:d, :p, :m)"
            ), {"d": d, "p": p, "m": m})
        conn.commit()
    yield eng


def test_extra_sources_unions_history(sqlite_engine):
    """F1: union primary + extra source, primary wins on date overlap."""
    from app.services.forecasting.edia_source import EdiaMysqlDataSource
    src = EdiaMysqlDataSource(engine=sqlite_engine)
    ds = {
        "source": "edia_mysql",
        "table": "md_t_lz_price",
        "time_col": "FDATE",
        "measure": "FTAXPRICE",
        "where": "FMATERIAL_NAME = 'isoprene-east'",
        "extra_sources": [
            {
                "table": "lz_v_异戊二烯_data",
                "time_col": "biz_date(业务日期)",
                "measure": "tax_price(含税单价)",
                "where": "`material_name(产品名称)` = '异戊二烯'",
            },
        ],
    }
    df = src.read_history(ds)
    # Should have 5 (primary) + 3 (extra, non-overlap) = 8 rows
    assert len(df) == 8, f"Expected 8 rows, got {len(df)}: {df}"
    # Primary wins on overlap (2026-07-02 should be 1001, not 999)
    row_0702 = df[df["ds"] == pd.Timestamp("2026-07-02")]
    assert float(row_0702["y"].iloc[0]) == 1001.0, row_0702
    # Extra fills the back-half (2025-03-01 should be 800)
    row_0301 = df[df["ds"] == pd.Timestamp("2025-03-01")]
    assert float(row_0301["y"].iloc[0]) == 800.0, row_0301
    # Sorted ascending
    assert df["ds"].is_monotonic_increasing


def test_extra_sources_empty_list_behaves_like_primary(sqlite_engine):
    """F1: no extra_sources = same as before."""
    from app.services.forecasting.edia_source import EdiaMysqlDataSource
    src = EdiaMysqlDataSource(engine=sqlite_engine)
    ds = {
        "source": "edia_mysql",
        "table": "md_t_lz_price",
        "time_col": "FDATE",
        "measure": "FTAXPRICE",
        "where": "FMATERIAL_NAME = 'isoprene-east'",
    }
    df = src.read_history(ds)
    assert len(df) == 5


def test_extra_sources_failure_does_not_break_primary(sqlite_engine):
    """F1: if extra source fails (table missing), primary still succeeds."""
    from app.services.forecasting.edia_source import EdiaMysqlDataSource
    src = EdiaMysqlDataSource(engine=sqlite_engine)
    ds = {
        "source": "edia_mysql",
        "table": "md_t_lz_price",
        "time_col": "FDATE",
        "measure": "FTAXPRICE",
        "where": "FMATERIAL_NAME = 'isoprene-east'",
        "extra_sources": [
            {
                "table": "nonexistent_table",
                "time_col": "FDATE",
                "measure": "FTAXPRICE",
                "where": "1=1",
            },
        ],
    }
    df = src.read_history(ds)
    # Should fall back to primary only
    assert len(df) == 5


def test_primary_failure_still_raises(sqlite_engine):
    """F1: if primary fails, error propagates (extras are best-effort)."""
    from app.services.forecasting.edia_source import EdiaMysqlDataSource
    src = EdiaMysqlDataSource(engine=sqlite_engine)
    ds = {
        "source": "edia_mysql",
        "table": "nonexistent_primary",
        "time_col": "FDATE",
        "measure": "FTAXPRICE",
        "where": "1=1",
        "extra_sources": [
            {
                "table": "lz_v_异戊二烯_data",
                "time_col": "biz_date(业务日期)",
                "measure": "tax_price(含税单价)",
                "where": "`material_name(产品名称)` = '异戊二烯'",
            },
        ],
    }
    with pytest.raises(RuntimeError):
        src.read_history(ds)