"""Tests for EdiaMysqlDataSource date deduplication — groupby("ds").agg({"y": "mean"})."""
from __future__ import annotations

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.services.forecasting.edia_source import EdiaMysqlDataSource


def _make_mock_engine_with_duplicates(rows: list[tuple]) -> Engine:
    """Build an in-memory SQLite engine with duplicate-date rows.

    Each row is (ds_str, y_value). Multiple rows with the same ds_str
    simulate ERP order entries landing on the same PLANDATE.
    """
    eng = create_engine("sqlite:///:memory:")
    with eng.connect() as conn:
        conn.execute(text(
            "CREATE TABLE erp_test (PLANDATE TEXT, FTAXPRICE TEXT, material_code TEXT)"
        ))
        for ds, y, code in rows:
            conn.execute(text(
                "INSERT INTO erp_test (PLANDATE, FTAXPRICE, material_code) "
                "VALUES (:d, :p, :c)"
            ), {"d": ds, "p": str(y), "c": code})
        conn.commit()
    return eng


def test_groupby_averages_duplicate_dates():
    """Multiple rows on the same date should be averaged into one row."""
    rows = [
        ("2026-07-01", 5000.0, "201000018"),
        ("2026-07-01", 7000.0, "201000018"),  # same date, different price
        ("2026-07-01", 6000.0, "201000018"),  # same date, third order
        ("2026-07-02", 5500.0, "201000018"),
    ]
    eng = _make_mock_engine_with_duplicates(rows)
    src = EdiaMysqlDataSource(engine=eng)
    df = src.read_history({
        "source": "edia_mysql",
        "table": "erp_test",
        "time_col": "PLANDATE",
        "measure": "FTAXPRICE",
        "where": "material_code = '201000018'",
        "granularity": "day",
    })
    # 3 rows for 2026-07-01 averaged → (5000+7000+6000)/3 = 6000.0
    # 1 row for 2026-07-02 → 5500.0
    assert len(df) == 2
    assert df.iloc[0]["ds"].strftime("%Y-%m-%d") == "2026-07-01"
    assert df.iloc[0]["y"] == 6000.0
    assert df.iloc[1]["ds"].strftime("%Y-%m-%d") == "2026-07-02"
    assert df.iloc[1]["y"] == 5500.0


def test_groupby_noop_when_dates_already_unique():
    """md_t_lz_price has duplicate dates with identical prices — avg is a no-op."""
    rows = [
        ("2026-07-01", 5500.0, "裂解C5-裂解C5均价"),
        ("2026-07-01", 5500.0, "裂解C5-裂解C5均价"),  # triplicate, same price
        ("2026-07-01", 5500.0, "裂解C5-裂解C5均价"),
        ("2026-07-02", 5600.0, "裂解C5-裂解C5均价"),
    ]
    eng = _make_mock_engine_with_duplicates(rows)
    src = EdiaMysqlDataSource(engine=eng)
    df = src.read_history({
        "source": "edia_mysql",
        "table": "erp_test",
        "time_col": "PLANDATE",
        "measure": "FTAXPRICE",
        "where": "material_code = '裂解C5-裂解C5均价'",
        "granularity": "day",
    })
    # Identical prices → avg = same value → no-op
    assert len(df) == 2
    assert df.iloc[0]["y"] == 5500.0
    assert df.iloc[1]["y"] == 5600.0


def test_groupby_preserves_sort_order():
    """After groupby, rows should still be sorted ascending by ds."""
    rows = [
        ("2026-07-03", 5700.0, "SKU1"),
        ("2026-07-01", 5500.0, "SKU1"),
        ("2026-07-01", 5600.0, "SKU1"),  # dup date
        ("2026-07-02", 5600.0, "SKU1"),
    ]
    eng = _make_mock_engine_with_duplicates(rows)
    src = EdiaMysqlDataSource(engine=eng)
    df = src.read_history({
        "source": "edia_mysql",
        "table": "erp_test",
        "time_col": "PLANDATE",
        "measure": "FTAXPRICE",
        "where": "material_code = 'SKU1'",
        "granularity": "day",
    })
    assert len(df) == 3
    assert df.iloc[0]["ds"] < df.iloc[1]["ds"] < df.iloc[2]["ds"]
