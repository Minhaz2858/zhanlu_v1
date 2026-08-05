"""Idempotent seeder for the Ecisco BI dashboard forecast targets.

Each target maps a dashboard product_id (e.g. 'crude_oil') to a primary
time series in EDIA's MySQL mirror. The 10 md_t_lz_price products are
seeded statically below; the 2 ERP products (c5_resin, raffinate_c5) are
discovered dynamically by discover_and_seed_sku_targets() — one target
per material_code (SKU) with >= 50 rows of history.

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
            "extra_sources": [
                {
                    "table": "lz_v_裂解c5_data",
                    "time_col": "biz_date(业务日期)",
                    "measure": "tax_price(含税单价)",
                    "where": "`material_name(产品名称)` = '裂解C5'",
                },
            ],
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
            "extra_sources": [
                {
                    "table": "lz_v_裂解c9_data",
                    "time_col": "biz_date(业务日期)",
                    "measure": "tax_price(含税单价)",
                    "where": "`material_name(产品名称)` = '裂解C9'",
                },
            ],
        },
        "include_in_weekly_report": True,
        "report_order": 4,
    },
    {
        # Dashboard product_id "mixed_c5" = Yangzi Cracked C5
        # (mirrors PRODUCT_TABLE_MAP["mixed_c5"] in models/market_dashboard.py)
        "product_key": "ecisco.mixed_c5",
        "name": "Yangzi Cracked C5",
        "datasource": {
            "source": "edia_mysql",
            "table": "md_t_lz_price",
            "time_col": "FDATE",
            "measure": "FTAXPRICE",
            "where": "FMATERIAL_NAME = '裂解C5-扬子裂解C5'",
            "granularity": "day",
        },
        "include_in_weekly_report": True,
        "report_order": 5,
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
            # Phase F1: union md_t_lz_price (2025-06+) with lz_v_*_data
            # (2025-03+) for ~300 rows of history. Primary wins on date
            # overlap; lz_v_*_data extends the back-half of the series.
            "extra_sources": [
                {
                    "table": "lz_v_异戊二烯_data",
                    "time_col": "biz_date(业务日期)",
                    "measure": "tax_price(含税单价)",
                    "where": "`material_name(产品名称)` = '异戊二烯'",
                },
            ],
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
            "extra_sources": [
                {
                    "table": "lz_v_间戊二烯_data",
                    "time_col": "biz_date(业务日期)",
                    "measure": "tax_price(含税单价)",
                    "where": "`material_name(产品名称)` = '间戊二烯'",
                },
            ],
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
            "extra_sources": [
                {
                    "table": "lz_v_双环戊二烯_data",
                    "time_col": "biz_date(业务日期)",
                    "measure": "tax_price(含税单价)",
                    "where": "`material_name(产品名称)` = '双环戊二烯'",
                },
            ],
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
            "extra_sources": [
                {
                    "table": "lz_v_sis_data",
                    "time_col": "biz_date(业务日期)",
                    "measure": "tax_price(含税单价)",
                    "where": "`material_name(产品名称)` = 'SIS'",
                },
            ],
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
            # Phase F1: extend history via lz_v_*_data view (Phase F1)
            "extra_sources": [
                {
                    "table": "lz_v_苯乙烯_data",
                    "time_col": "biz_date(业务日期)",
                    "measure": "tax_price(含税单价)",
                    "where": "`material_name(产品名称)` = '苯乙烯'",
                },
            ],
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
            "extra_sources": [
                {
                    "table": "lz_v_戊烷泡发剂_data",
                    "time_col": "biz_date(业务日期)",
                    "measure": "tax_price(含税单价)",
                    "where": "`material_name(产品名称)` = '戊烷发泡剂'",
                },
            ],
        },
        "include_in_weekly_report": True,
        "report_order": 11,
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


# ── ERP product families that need dynamic SKU discovery ───────────────
# Each entry maps a dashboard product_id to its ERP table name.
# The seeder queries SELECT DISTINCT material_code from these tables
# and creates one ForecastTarget per SKU (material_code) with >= 50 rows.
_ERP_PRODUCT_TABLES: dict[str, str] = {
    "c5_resin":     "sale_erp_v_碳五石油树脂_data",
    "raffinate_c5": "sale_erp_v_抽余碳五_data",
}

# Minimum row count for a SKU to be forecastable.
_MIN_SKU_ROWS = 50


def discover_and_seed_sku_targets(
    db: Session,
    org_id: str = "default-org",
    app_id: str = "default-app",
    engine=None,
) -> int:
    """Discover SKUs (material_code values) in ERP tables and seed one
    ForecastTarget per SKU.

    For each ERP product family (c5_resin, raffinate_c5):
    1. Query SELECT DISTINCT material_code, COUNT(*) FROM <table>
       WHERE FTAXPRICE > 0 GROUP BY material_code
    2. Filter to SKUs with >= 50 rows (minimum for forecasting)
    3. Sort by row count descending — primary SKU gets report_order=12,
       secondary SKUs get 13, 14, ...
    4. Insert ForecastTarget rows with product_key="ecisco.<family>.<code>"

    Idempotent: skips targets that already exist (matched by product_key
    + org_id). Safe to call on every boot.

    Args:
        db: SQLAlchemy session.
        org_id: Org ID for the targets.
        app_id: App ID for the targets.
        engine: Optional SQLAlchemy engine for the ERP MySQL mirror.
                If None, uses get_mysql_engine().

    Returns:
        Number of new ForecastTarget rows inserted.
    """
    from sqlalchemy import text as sa_text

    if engine is None:
        from app.core.mysql_db import get_mysql_engine
        engine = get_mysql_engine()
    if engine is None:
        logger.warning(
            "discover_and_seed_sku_targets: EDIA MySQL unreachable, "
            "skipping SKU discovery."
        )
        return 0

    inserted = 0
    for family, table_name in _ERP_PRODUCT_TABLES.items():
        try:
            with engine.connect() as conn:
                # Discover SKUs: material_code + row count, filtered by
                # FTAXPRICE > 0 (exclude cancelled/reversed entries).
                stmt = sa_text(
                    f"SELECT material_code, COUNT(*) AS cnt "
                    f"FROM `{table_name}` "
                    f"WHERE FTAXPRICE > 0 "
                    f"AND material_code IS NOT NULL "
                    f"AND material_code != '' "
                    f"GROUP BY material_code "
                    f"ORDER BY cnt DESC"
                )
                rows = conn.execute(stmt).fetchall()
        except Exception as exc:
            logger.warning(
                "discover_and_seed_sku_targets: query failed for %s: %s",
                table_name, exc,
            )
            continue

        # Filter to SKUs with enough history
        qualified = [
            (code, cnt) for code, cnt in rows
            if cnt >= _MIN_SKU_ROWS
        ]
        if not qualified:
            logger.info(
                "discover_and_seed_sku_targets: no SKUs with >= %d rows in %s",
                _MIN_SKU_ROWS, table_name,
            )
            continue

        logger.info(
            "discover_and_seed_sku_targets: %s — %d SKUs qualified (of %d total)",
            family, len(qualified), len(rows),
        )

        for idx, (code, cnt) in enumerate(qualified):
            product_key = f"ecisco.{family}.{code}"
            # Primary SKU (idx=0) gets report_order=12 (family's original).
            # Secondary SKUs get 13, 14, 15, ...
            report_order = 12 + idx

            existing = db.query(ForecastTarget).filter(
                ForecastTarget.product_key == product_key,
                ForecastTarget.org_id == org_id,
                ForecastTarget.is_deleted == False,  # noqa: E712
            ).first()
            if existing is not None:
                continue

            target = ForecastTarget(
                org_id=org_id,
                app_id=app_id,
                product_key=product_key,
                name=f"{family} SKU {code} (ERP, {cnt} rows)",
                datasource={
                    "source": "edia_mysql",
                    "table": table_name,
                    "time_col": "PLANDATE",
                    "measure": "FTAXPRICE",
                    "where": f"material_code = '{code}' AND FTAXPRICE > 0",
                    "granularity": "day",
                },
                level=2,
                quality_grade="B",
                status="active",
                source="seed_sku",
                include_in_weekly_report=(idx == 0),  # only primary SKU
                report_order=report_order,
            )
            db.add(target)
            inserted += 1

    if inserted:
        db.commit()
        logger.info(
            "discover_and_seed_sku_targets: inserted %d new SKU targets for org=%s",
            inserted, org_id,
        )
    return inserted
