"""Tests for dynamic SKU discovery seeder — replaces static ERP WHERE 1=1 targets."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.forecasting import ForecastTarget
from app.services.domain_config import get_domain_config
from app.services.forecasting.seed_targets import (
    discover_and_seed_sku_targets,
    seed_forecast_targets,
)

ECISCO_FORECAST_TARGETS = get_domain_config("").get("forecast_targets", [])


@pytest.fixture
def db_session():
    """In-memory SQLite session with forecasting tables created."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng, tables=[ForecastTarget.__table__])
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()


def _make_mock_erp_engine(rows: list[tuple]) -> object:
    """Build an in-memory SQLite engine simulating sale_erp_v_碳五石油树脂_data.

    Each row is (material_code, plandate, ftaxprice).
    """
    eng = create_engine("sqlite:///:memory:")
    with eng.connect() as conn:
        conn.execute(text(
            "CREATE TABLE sale_erp_v_碳五石油树脂_data "
            "(material_code TEXT, PLANDATE TEXT, FTAXPRICE REAL)"
        ))
        for code, dt, price in rows:
            conn.execute(text(
                "INSERT INTO sale_erp_v_碳五石油树脂_data "
                "(material_code, PLANDATE, FTAXPRICE) VALUES (:c, :d, :p)"
            ), {"c": code, "d": dt, "p": price})
        conn.commit()
    return eng


def test_static_targets_no_longer_include_erp_where_1_1():
    """ERP-primary targets are discovered dynamically by the SKU seeder;
    no static target may use a degenerate WHERE 1=1 datasource."""
    degenerate = [
        t["product_key"] for t in ECISCO_FORECAST_TARGETS
        if "1=1" in str(t["datasource"].get("where", ""))
    ]
    assert len(degenerate) == 0, (
        f"ERP products {erp_keys} should not be in static list — "
        "they are discovered dynamically by discover_and_seed_sku_targets()"
    )


def test_static_targets_still_has_10_md_lz_products():
    """The md_t_lz_price-primary products remain in the static list."""
    md_lz_count = sum(
        1 for t in ECISCO_FORECAST_TARGETS
        if t["datasource"].get("table") == "md_t_lz_price"
    )
    assert md_lz_count == 6  # 6 of 11 static targets use md_t_lz_price as primary


def test_discover_skus_inserts_one_target_per_sku(db_session):
    """discover_and_seed_sku_targets should insert one ForecastTarget per
    material_code with >= 50 rows."""
    # SKU A: 60 rows (above threshold)
    # SKU B: 30 rows (below threshold — skipped)
    # SKU C: 50 rows (exactly at threshold — included)
    rows = []
    for i in range(60):
        rows.append(("SKU_A", f"2025-01-{i+1:02d}", 5000.0 + i))
    for i in range(30):
        rows.append(("SKU_B", f"2025-01-{i+1:02d}", 3000.0 + i))
    for i in range(50):
        rows.append(("SKU_C", f"2025-01-{i+1:02d}", 7000.0 + i))
    # Add some zero-price rows (should be filtered by FTAXPRICE > 0)
    rows.append(("SKU_A", "2025-03-01", 0.0))

    erp_eng = _make_mock_erp_engine(rows)
    count = discover_and_seed_sku_targets(
        db_session, org_id="test-org", app_id="test-app", engine=erp_eng
    )
    # SKU_A (60 rows) + SKU_C (50 rows) = 2 targets. SKU_B (30) skipped.
    assert count == 2

    targets = db_session.query(ForecastTarget).filter(
        ForecastTarget.org_id == "test-org",
        ForecastTarget.product_key.like("ecisco.c5_resin.%"),
    ).all()
    assert len(targets) == 2
    keys = {t.product_key for t in targets}
    assert "ecisco.c5_resin.SKU_A" in keys
    assert "ecisco.c5_resin.SKU_C" in keys
    assert "ecisco.c5_resin.SKU_B" not in keys


def test_discover_skus_assigns_primary_sku_lowest_report_order(db_session):
    """The SKU with the most rows gets report_order=12 (the family's
    original value). Secondary SKUs get 13, 14, ..."""
    rows = []
    for i in range(100):
        rows.append(("PRIMARY", f"2025-01-{(i % 28) + 1:02d}", 5000.0 + i))
    for i in range(60):
        rows.append(("SECONDARY", f"2025-01-{(i % 28) + 1:02d}", 6000.0 + i))

    erp_eng = _make_mock_erp_engine(rows)
    discover_and_seed_sku_targets(
        db_session, org_id="test-org", app_id="test-app", engine=erp_eng
    )

    primary = db_session.query(ForecastTarget).filter(
        ForecastTarget.product_key == "ecisco.c5_resin.PRIMARY"
    ).one()
    secondary = db_session.query(ForecastTarget).filter(
        ForecastTarget.product_key == "ecisco.c5_resin.SECONDARY"
    ).one()
    assert primary.report_order == 12
    assert secondary.report_order == 13


def test_discover_skus_where_clause_filters_by_material_code(db_session):
    """Each SKU target's datasource.where should filter by material_code."""
    rows = []
    for i in range(55):
        rows.append(("CODE_123", f"2025-01-{(i % 28) + 1:02d}", 5000.0 + i))

    erp_eng = _make_mock_erp_engine(rows)
    discover_and_seed_sku_targets(
        db_session, org_id="test-org", app_id="test-app", engine=erp_eng
    )

    target = db_session.query(ForecastTarget).filter(
        ForecastTarget.product_key == "ecisco.c5_resin.CODE_123"
    ).one()
    assert "material_code = 'CODE_123'" in target.datasource["where"]
    assert "FTAXPRICE > 0" in target.datasource["where"]


def test_discover_skus_is_idempotent(db_session):
    """Second call should insert 0 new targets."""
    rows = []
    for i in range(55):
        rows.append(("SKU_X", f"2025-01-{(i % 28) + 1:02d}", 5000.0 + i))

    erp_eng = _make_mock_erp_engine(rows)
    first = discover_and_seed_sku_targets(
        db_session, org_id="test-org", app_id="test-app", engine=erp_eng
    )
    second = discover_and_seed_sku_targets(
        db_session, org_id="test-org", app_id="test-app", engine=erp_eng
    )
    assert first >= 1
    assert second == 0


def test_discover_skus_handles_both_erp_products(db_session):
    """Both c5_resin and raffinate_c5 should be discovered."""
    eng = create_engine("sqlite:///:memory:")
    with eng.connect() as conn:
        conn.execute(text(
            "CREATE TABLE sale_erp_v_碳五石油树脂_data "
            "(material_code TEXT, PLANDATE TEXT, FTAXPRICE REAL)"
        ))
        conn.execute(text(
            "CREATE TABLE sale_erp_v_抽余碳五_data "
            "(material_code TEXT, PLANDATE TEXT, FTAXPRICE REAL)"
        ))
        for i in range(55):
            conn.execute(text(
                "INSERT INTO sale_erp_v_碳五石油树脂_data VALUES (:c, :d, :p)"
            ), {"c": "RESIN_1", "d": f"2025-01-{(i % 28) + 1:02d}", "p": 5000.0 + i})
        for i in range(55):
            conn.execute(text(
                "INSERT INTO sale_erp_v_抽余碳五_data VALUES (:c, :d, :p)"
            ), {"c": "RAFF_1", "d": f"2025-01-{(i % 28) + 1:02d}", "p": 4000.0 + i})
        conn.commit()

    count = discover_and_seed_sku_targets(
        db_session, org_id="test-org", app_id="test-app", engine=eng
    )
    assert count == 2
    resin = db_session.query(ForecastTarget).filter(
        ForecastTarget.product_key == "ecisco.c5_resin.RESIN_1"
    ).one()
    raff = db_session.query(ForecastTarget).filter(
        ForecastTarget.product_key == "ecisco.raffinate_c5.RAFF_1"
    ).one()
    assert resin.report_order == 12
    assert raff.report_order == 12  # both families start at 12