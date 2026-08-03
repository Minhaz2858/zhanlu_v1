"""Idempotent seeder for the 12 Ecisco BI dashboard forecast targets.

Each target maps a dashboard product_id (e.g. 'crude_oil') to a primary
time series in EDIA's MySQL mirror (md_t_lz_price for 10 products, ERP
sale_erp_v_*_data for raffinate_c5 and c5_resin).

Run on boot via seed.py, or manually via the /bootstrap-nightly-forecast
endpoint. Safe to re-run — skips targets that already exist (matched by
product_key + org_id).
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.forecasting import ForecastTarget

logger = logging.getLogger(__name__)


# ── Product → primary series mapping ────────────────────────────────────
# Mirrors PRODUCT_TABLE_MAP primary config in models/market_dashboard.py.
# Each entry has a datasource descriptor that EdiaMysqlDataSource reads.
ECISCO_FORECAST_TARGETS: list[dict] = [
    {
        "product_key": "ecisco.crude_oil",
        "name": "Crude Oil (Brent)",
        "datasource": {
            "source": "edia_mysql",
            "table": "md_t_lz_price",
            "time_col": "FDATE",
            "measure": "FTAXPRICE",
            "where": "FMATERIAL_NAME = '裂解C5-布伦特'",
            "granularity": "day",
        },
        "include_in_weekly_report": True,
        "report_order": 1,
    },
    {
        "product_key": "ecisco.naphtha",
        "name": "Naphtha (Japan)",
        "datasource": {
            "source": "edia_mysql",
            "table": "md_t_lz_price",
            "time_col": "FDATE",
            "measure": "FTAXPRICE",
            "where": "FMATERIAL_NAME = '裂解C5-日本石脑油'",
            "granularity": "day",
        },
        "include_in_weekly_report": True,
        "report_order": 2,
    },
    {
        "product_key": "ecisco.cracked_c5",
        "name": "Cracked C5 (Average)",
        "datasource": {
            "source": "edia_mysql",
            "table": "md_t_lz_price",
            "time_col": "FDATE",
            "measure": "FTAXPRICE",
            "where": "FMATERIAL_NAME = '裂解C5-裂解C5均价'",
            "granularity": "day",
        },
        "include_in_weekly_report": True,
        "report_order": 3,
    },
    {
        "product_key": "ecisco.cracked_c9",
        "name": "Cracked C9 (Average)",
        "datasource": {
            "source": "edia_mysql",
            "table": "md_t_lz_price",
            "time_col": "FDATE",
            "measure": "FTAXPRICE",
            "where": "FMATERIAL_NAME = '裂解C9-裂解C9均价'",
            "granularity": "day",
        },
        "include_in_weekly_report": True,
        "report_order": 4,
    },
    {
        "product_key": "ecisco.isoprene",
        "name": "Isoprene (East China)",
        "datasource": {
            "source": "edia_mysql",
            "table": "md_t_lz_price",
            "time_col": "FDATE",
            "measure": "FTAXPRICE",
            "where": "FMATERIAL_NAME = '异戊二烯-华东市场价'",
            "granularity": "day",
        },
        "include_in_weekly_report": True,
        "report_order": 6,
    },
    {
        "product_key": "ecisco.piperylene",
        "name": "Piperylene (East China)",
        "datasource": {
            "source": "edia_mysql",
            "table": "md_t_lz_price",
            "time_col": "FDATE",
            "measure": "FTAXPRICE",
            "where": "FMATERIAL_NAME = '间戊二烯-华东市场价'",
            "granularity": "day",
        },
        "include_in_weekly_report": True,
        "report_order": 6,
    },
    {
        "product_key": "ecisco.dcpd",
        "name": "DCPD (East China)",
        "datasource": {
            "source": "edia_mysql",
            "table": "md_t_lz_price",
            "time_col": "FDATE",
            "measure": "FTAXPRICE",
            "where": "FMATERIAL_NAME = '双环戊二烯-华东市场价'",
            "granularity": "day",
        },
        "include_in_weekly_report": True,
        "report_order": 7,
    },
    {
        "product_key": "ecisco.sis",
        "name": "SIS (Baling 1105)",
        "datasource": {
            "source": "edia_mysql",
            "table": "md_t_lz_price",
            "time_col": "FDATE",
            "measure": "FTAXPRICE",
            "where": "FMATERIAL_NAME = 'SIS-巴陵1105'",
            "granularity": "day",
        },
        "include_in_weekly_report": True,
        "report_order": 9,
    },
    {
        "product_key": "ecisco.styrene",
        "name": "Styrene (Sinopec East China)",
        "datasource": {
            "source": "edia_mysql",
            "table": "md_t_lz_price",
            "time_col": "FDATE",
            "measure": "FTAXPRICE",
            "where": "FMATERIAL_NAME = '苯乙烯-中石化华东'",
            "granularity": "day",
        },
        "include_in_weekly_report": True,
        "report_order": 9,
    },
    {
        "product_key": "ecisco.blowing_agent",
        "name": "Blowing Agent (East China)",
        "datasource": {
            "source": "edia_mysql",
            "table": "md_t_lz_price",
            "time_col": "FDATE",
            "measure": "FTAXPRICE",
            "where": "FMATERIAL_NAME = '戊烷发泡剂-华东市场价'",
            "granularity": "day",
        },
        "include_in_weekly_report": True,
        "report_order": 11,
    },
    # ERP fallback products — no FMATERIAL_NAME filter; these tables have
    # PLANDATE / FTAXPRICE columns and no supplier dimension.
    {
        "product_key": "ecisco.c5_resin",
        "name": "C5 Resin (ERP Order Book)",
        "datasource": {
            "source": "edia_mysql",
            "table": "sale_erp_v_碳五石油树脂_data",
            "time_col": "PLANDATE",
            "measure": "FTAXPRICE",
            "where": "1=1",
            "granularity": "day",
        },
        "include_in_weekly_report": True,
        "report_order": 12,
    },
    {
        "product_key": "ecisco.raffinate_c5",
        "name": "Raffinate C5 (ERP Order Book)",
        "datasource": {
            "source": "edia_mysql",
            "table": "sale_erp_v_抽余碳五_data",
            "time_col": "PLANDATE",
            "measure": "FTAXPRICE",
            "where": "1=1",
            "granularity": "day",
        },
        "include_in_weekly_report": True,
        "report_order": 12,
    },
]


def seed_ecisco_forecast_targets(
    db: Session,
    org_id: str = "default-org",
    app_id: str = "default-app",
) -> int:
    """Insert any missing Ecisco BI forecast targets. Returns count of new rows.

    Idempotent: skips targets that already exist (matched by product_key +
    org_id). Safe to call on every boot.
    """
    inserted = 0
    for spec in ECISCO_FORECAST_TARGETS:
        existing = db.query(ForecastTarget).filter(
            ForecastTarget.product_key == spec["product_key"],
            ForecastTarget.org_id == org_id,
            ForecastTarget.is_deleted == False,  # noqa: E712
        ).first()
        if existing is not None:
            continue

        target = ForecastTarget(
            org_id=org_id,
            app_id=app_id,
            product_key=spec["product_key"],
            name=spec["name"],
            datasource=spec["datasource"],
            level=2,  # pack-grade
            quality_grade="B",  # default; re-scored on first compute_target
            status="active",
            source="seed",
            include_in_weekly_report=spec["include_in_weekly_report"],
            report_order=spec["report_order"],
        )
        db.add(target)
        inserted += 1

    if inserted:
        db.commit()
        logger.info(
            "seed_ecisco_forecast_targets: inserted %d new targets for org=%s",
            inserted, org_id,
        )
    return inserted
