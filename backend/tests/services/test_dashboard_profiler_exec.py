import sqlite3

import pytest

from app.services.dashboard_profiler import profile_engine


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "profile.db"
    con = sqlite3.connect(p)
    con.executescript(
        """
        CREATE TABLE sales_orders (
            product TEXT,
            amount REAL,
            created_at TEXT,
            region TEXT
        );
        INSERT INTO sales_orders VALUES
            ('C5 Resin', 120.5, '2026-01-05', 'East'),
            ('C5 Resin', 99.0, '2026-02-10', 'East'),
            ('Isoprene', 250.0, '2026-03-15', 'West'),
            ('C5 Resin', NULL, '2026-04-20', NULL),
            ('C9 Resin', 88.0, '2026-05-25', 'East');
        """
    )
    con.commit()
    con.close()
    return str(p)


def test_profile_engine_returns_expected_shape(db_path):
    result = profile_engine(db_path, "sales_orders", ["product", "amount", "created_at", "region"])
    assert result["table"] == "sales_orders"
    assert result["row_count"] == 5
    assert result["status"] == "ok"
    cols = {c["name"]: c for c in result["columns"]}
    # Fixture has 3 distinct products: C5 Resin (3 rows), Isoprene, C9 Resin.
    # (Plan draft said 4 — corrected to match the actual fixture data.)
    assert cols["product"]["cardinality"] == 3
    assert cols["product"]["null_pct"] == 0.0
    assert cols["product"]["shape"] == "category"
    assert cols["amount"]["null_pct"] == pytest.approx(0.2)
    assert cols["created_at"]["shape"] == "time_series"
    assert len(cols["region"]["top_values"]) >= 2


def test_profile_engine_missing_table_reports_error(db_path):
    result = profile_engine(db_path, "no_such_table", ["a"])
    assert result["status"] == "error"
    assert result["error_message"]


def test_profile_engine_unknown_column_reports_error(db_path):
    result = profile_engine(db_path, "sales_orders", ["nope"])
    assert result["status"] == "error"
    assert result["error_message"]
