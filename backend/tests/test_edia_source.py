"""Tests for EdiaMysqlDataSource — reads time series from EDIA MySQL mirror."""
from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.services.forecasting.edia_source import EdiaMysqlDataSource


def _make_mock_engine(rows: list[tuple]) -> Engine:
    """Build an in-memory SQLite engine with a temp table populated with `rows`.

    Each row is (ds_str, y_value_or_sentinel, material_name). We use SQLite
    with a temp table so EdiaMysqlDataSource's raw SQL works against a real
    engine (SQLite uses backtick-compatible double-quote identifier quoting
    in practice, but our SQL uses backticks — SQLite accepts backticks as
    identifier quotes too, so this works)."""
    eng = create_engine("sqlite:///:memory:")
    with eng.connect() as conn:
        conn.execute(text(
            "CREATE TABLE md_t_lz_price (FDATE TEXT, FTAXPRICE TEXT, FMATERIAL_NAME TEXT)"
        ))
        for ds, y, name in rows:
            conn.execute(text(
                "INSERT INTO md_t_lz_price (FDATE, FTAXPRICE, FMATERIAL_NAME) "
                "VALUES (:d, :p, :n)"
            ), {"d": ds, "p": str(y), "n": name})
        conn.commit()
    return eng


def test_read_history_returns_dataframe_with_ds_y_columns():
    rows = [
        ("2026-07-01", 5500.0, "裂解C5-裂解C5均价"),
        ("2026-07-02", 5600.0, "裂解C5-裂解C5均价"),
        ("2026-07-03", 5700.0, "裂解C5-裂解C5均价"),
    ]
    eng = _make_mock_engine(rows)
    src = EdiaMysqlDataSource(engine=eng)
    df = src.read_history({
        "source": "edia_mysql",
        "table": "md_t_lz_price",
        "time_col": "FDATE",
        "measure": "FTAXPRICE",
        "where": "FMATERIAL_NAME = '裂解C5-裂解C5均价'",
        "granularity": "day",
    })
    assert list(df.columns) == ["ds", "y"]
    assert len(df) == 3
    assert df["y"].dtype == float
    assert df.iloc[0]["y"] == 5500.0


def test_read_history_drops_sentinel_values_F7_and_NaN():
    rows = [
        ("2026-07-01", 5500.0, "裂解C5-裂解C5均价"),
        ("2026-07-02", "F7", "裂解C5-裂解C5均价"),       # sentinel — drop
        ("2026-07-03", "NaN", "裂解C5-裂解C5均价"),       # sentinel — drop
        ("2026-07-04", "", "裂解C5-裂解C5均价"),          # empty — drop
        ("2026-07-05", 5700.0, "裂解C5-裂解C5均价"),
    ]
    eng = _make_mock_engine(rows)
    src = EdiaMysqlDataSource(engine=eng)
    df = src.read_history({
        "source": "edia_mysql",
        "table": "md_t_lz_price",
        "time_col": "FDATE",
        "measure": "FTAXPRICE",
        "where": "FMATERIAL_NAME = '裂解C5-裂解C5均价'",
        "granularity": "day",
    })
    # 5500 (keep), F7 (drop), NaN (drop), "" (drop), 5700 (keep) → 2 rows
    assert len(df) == 2
    assert df.iloc[0]["y"] == 5500.0
    assert df.iloc[1]["y"] == 5700.0


def test_read_history_sorted_ascending_by_ds():
    rows = [
        ("2026-07-03", 5700.0, "裂解C5-裂解C5均价"),
        ("2026-07-01", 5500.0, "裂解C5-裂解C5均价"),
        ("2026-07-02", 5600.0, "裂解C5-裂解C5均价"),
    ]
    eng = _make_mock_engine(rows)
    src = EdiaMysqlDataSource(engine=eng)
    df = src.read_history({
        "source": "edia_mysql",
        "table": "md_t_lz_price",
        "time_col": "FDATE",
        "measure": "FTAXPRICE",
        "where": "FMATERIAL_NAME = '裂解C5-裂解C5均价'",
        "granularity": "day",
    })
    assert df.iloc[0]["ds"] < df.iloc[1]["ds"] < df.iloc[2]["ds"]


def test_read_history_raises_when_engine_is_none(monkeypatch):
    # Patch get_mysql_engine so _resolve_engine returns None even when
    # EDIA_MYSQL_URL is set in the environment.
    monkeypatch.setattr(
        "app.core.mysql_db.get_mysql_engine", lambda: None
    )
    src = EdiaMysqlDataSource()  # no engine injected → falls back to get_mysql_engine
    with pytest.raises(RuntimeError, match="EDIA MySQL unreachable"):
        src.read_history({
            "source": "edia_mysql",
            "table": "md_t_lz_price",
            "time_col": "FDATE",
            "measure": "FTAXPRICE",
            "where": "1=1",
            "granularity": "day",
        })


def test_read_history_raises_when_no_rows_returned():
    eng = _make_mock_engine([])  # empty table
    src = EdiaMysqlDataSource(engine=eng)
    with pytest.raises(RuntimeError, match="No rows"):
        src.read_history({
            "source": "edia_mysql",
            "table": "md_t_lz_price",
            "time_col": "FDATE",
            "measure": "FTAXPRICE",
            "where": "FMATERIAL_NAME = 'NONEXISTENT'",
            "granularity": "day",
        })
