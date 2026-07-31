"""One-time seed for ForecastTarget rows in zhanlu's Postgres.

Registers 9 products that have data in the MySQL warehouse's LZ views.
The 7 remaining pack products (naphtha, raffinate_c5, c5_resin, hydrogenated_c9,
crude_oil, butadiene, sbs) have no warehouse data and intentionally get no
ForecastTarget row — forecast_run will return "not found" and the truth_gate
(Task 5) will surface that honestly.
"""

from __future__ import annotations

import os

from sqlalchemy.orm import Session

from app.models.forecasting import ForecastTarget

# KB id for the MySQL warehouse. Override via env var to seed against a
# different KB (e.g. for testing).
DEFAULT_WAREHOUSE_KB_ID = "650a2505-3012-4968-87bf-80d79ac27da8"

# product_key → (lz_view_table, material_name, time_column, measure)
SEED_PRODUCTS = [
    ("cracked_c5",       "lz_v_裂解c5_data",      "裂解C5",   "biz_date(业务日期)", "tax_price(含税单价)"),
    ("cracked_c9",       "lz_v_裂解c9_data",      "裂解C9",   "biz_date(业务日期)", "tax_price(含税单价)"),
    ("isoprene",         "lz_v_异戊二烯_data",    "异戊二烯", "biz_date(业务日期)", "tax_price(含税单价)"),
    ("piperylene",       "lz_v_间戊二烯_data",    "间戊二烯", "biz_date(业务日期)", "tax_price(含税单价)"),
    ("dcpd",             "lz_v_双环戊二烯_data",  "双环戊二烯", "biz_date(业务日期)", "tax_price(含税单价)"),
    ("sis",              "lz_v_sis_data",         "SIS",      "biz_date(业务日期)", "tax_price(含税单价)"),
    ("blowing_agent",    "lz_v_戊烷泡发剂_data",  "戊烷泡发剂", "biz_date(业务日期)", "tax_price(含税单价)"),
    ("trimethylbenzene", "lz_v_混三甲苯_data",    "混三甲苯", "biz_date(业务日期)", "tax_price(含税单价)"),
    ("styrene",          "lz_v_苯乙烯_data",      "苯乙烯",   "biz_date(业务日期)", "tax_price(含税单价)"),
]


def seed_forecast_targets(
    db: Session,
    org_id: str = "default-org",
    app_id: str = "default-app",
    kb_id: str | None = None,
) -> int:
    """Idempotently create ForecastTarget rows for the 9 products with data.

    Returns the number of rows created (excluding pre-existing).
    """
    if kb_id is None:
        kb_id = os.environ.get("WAREHOUSE_KB_ID", DEFAULT_WAREHOUSE_KB_ID)

    created = 0
    for product_key, table, material, time_col, measure in SEED_PRODUCTS:
        existing = (
            db.query(ForecastTarget)
            .filter(
                ForecastTarget.product_key == product_key,
                ForecastTarget.org_id == org_id,
            )
            .first()
        )
        if existing is not None:
            continue
        target = ForecastTarget(
            org_id=org_id,
            app_id=app_id,
            product_key=product_key,
            name=product_key.replace("_", " ").title(),
            datasource={
                "kb_id": kb_id,
                "table": table,
                "time_column": time_col,
                "measure": measure,
                "dimensions": ["material_name(产品名称)"],
                "material_filter": material,
            },
        )
        db.add(target)
        created += 1
    db.commit()
    return created
