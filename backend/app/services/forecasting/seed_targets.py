"""Idempotent seeder for forecast targets — reads definitions from per-app domain config.

For apps WITH a domain config carrying ``forecast_targets`` (a list of
{product_key, name, datasource, include_in_weekly_report, report_order}),
seeds one ForecastTarget row per definition (matched by product_key + org_id).
Apps WITHOUT a config get NOTHING seeded — fully generic: targets are created
by the user or by their own integration.

Optional SKU discovery: when the config carries ``erp_sku_targets``
({family: table_name}), discovers ``material_code`` values per table and seeds
one target per SKU with enough history (``forecast_min_sku_rows``, default 50).
``forecast_key_prefix`` (default "") namespaces product keys when the app's
data model needs it.

Run on boot via seed.py, or manually via the /bootstrap-nightly-forecast
endpoint. Safe to re-run — skips targets that already exist.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.forecasting import ForecastTarget
from app.services.domain_config import get_domain_config

logger = logging.getLogger(__name__)

DEFAULT_AGENT_NAME = ""


def seed_forecast_targets(
    db: Session,
    org_id: str = "default-org",
    app_id: str = "default-app",
    agent_name: str | None = None,
) -> int:
    """Insert any missing forecast targets from the app's domain config.

    Returns the count of new rows. Idempotent: skips targets that already
    exist (matched by product_key + org_id). Apps without config seed 0.
    """
    agent_name = agent_name or DEFAULT_AGENT_NAME
    specs = get_domain_config(agent_name).get("forecast_targets", [])
    if not specs:
        logger.debug("seed_forecast_targets: no forecast_targets in config for %s", agent_name)
        return 0

    inserted = 0
    for spec in specs:
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
            include_in_weekly_report=spec.get("include_in_weekly_report", True),
            report_order=spec.get("report_order", 0),
        )
        db.add(target)
        inserted += 1

    if inserted:
        db.commit()
        logger.info(
            "seed_forecast_targets: inserted %d new targets for org=%s (agent=%s)",
            inserted, org_id, agent_name,
        )
    return inserted


def discover_and_seed_sku_targets(
    db: Session,
    org_id: str = "default-org",
    app_id: str = "default-app",
    engine=None,
    agent_name: str | None = None,
) -> int:
    """Discover SKUs (material_code values) in ERP tables and seed one
    ForecastTarget per SKU.

    The family → table map comes from the app's domain config
    (``erp_sku_targets``); absent → no SKU discovery (generic apps).

    1. Query SELECT DISTINCT material_code, COUNT(*) FROM <table>
       WHERE FTAXPRICE > 0 GROUP BY material_code
    2. Filter to SKUs with >= min rows (``forecast_min_sku_rows``, default 50)
    3. Sort by row count descending — primary SKU gets report_order=12,
       secondary SKUs get 13, 14, ...
    4. Insert ForecastTarget rows with product_key="<prefix><family>.<code>"

    Idempotent: skips targets that already exist (matched by product_key
    + org_id). Safe to call on every boot.
    """
    from sqlalchemy import text as sa_text

    agent_name = agent_name or DEFAULT_AGENT_NAME
    cfg = get_domain_config(agent_name)
    erp_tables: dict[str, str] = cfg.get("erp_sku_targets", {})
    if not erp_tables:
        logger.debug("discover_and_seed_sku_targets: no erp_sku_targets in config for %s", agent_name)
        return 0

    prefix: str = cfg.get("forecast_key_prefix", "")
    min_rows: int = int(cfg.get("forecast_min_sku_rows", 50))

    if engine is None:
        from app.core.mysql_db import get_mysql_engine
        engine = get_mysql_engine()
    if engine is None:
        logger.warning(
            "discover_and_seed_sku_targets: MySQL mirror unreachable, "
            "skipping SKU discovery."
        )
        return 0

    inserted = 0
    for family, table_name in erp_tables.items():
        try:
            with engine.connect() as conn:
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

        qualified = [
            (code, cnt) for code, cnt in rows
            if cnt >= min_rows
        ]
        if not qualified:
            logger.info(
                "discover_and_seed_sku_targets: no SKUs with >= %d rows in %s",
                min_rows, table_name,
            )
            continue

        logger.info(
            "discover_and_seed_sku_targets: %s — %d SKUs qualified (of %d total)",
            family, len(qualified), len(rows),
        )

        for idx, (code, cnt) in enumerate(qualified):
            product_key = f"{prefix}{family}.{code}"
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

            db.add(ForecastTarget(
                org_id=org_id,
                app_id=app_id,
                product_key=product_key,
                name=f"{family} SKU {code}",
                datasource={
                    "source": "edia_mysql",
                    "table": table_name,
                    "time_col": "PLANDATE",
                    "measure": "FTAXPRICE",
                    "where": f"material_code = '{code}' AND FTAXPRICE > 0",
                    "granularity": "day",
                },
                level=3,  # SKU-grade
                quality_grade="B",
                status="active",
                source="seed_sku",
                include_in_weekly_report=True,
                report_order=report_order,
            ))
            inserted += 1

    if inserted:
        db.commit()
        logger.info(
            "discover_and_seed_sku_targets: inserted %d new SKU targets for org=%s (agent=%s)",
            inserted, org_id, agent_name,
        )
    return inserted
