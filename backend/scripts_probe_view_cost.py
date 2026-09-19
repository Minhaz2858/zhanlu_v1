"""Why the semantically BEST-matching tables are the ones that fail.

`scripts_probe_data_truth.py` shows the agent's "no data" answers were true for
the table it chose (erp_product_sales_details: 0 rows after 2025-12-31) while 8
other tables carry in-period rows.

This script measures the other half: the two views named exactly like the
business question ("合同执行情况") were interrupted at the 10 s statement cap.
How long do they actually need, and what do they cost?

    docker exec zhanlu-copy-backend sh -lc 'cd /app && python scripts_probe_view_cost.py > /tmp/vc.out 2>/dev/null'
"""
from __future__ import annotations

import time

from sqlalchemy import text

from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.services.db.engine_cache import acquire_engine

CANDIDATES = [
    "erp_v_contract",
    "erp_v_contract_execution",
    "erp_t_crm_contract",
    "erp_t_sal_deliverynotice",
    "erp_t_sal_outstock",
]

PROBES = [
    ("count(*)", "SELECT COUNT(*) c FROM {t}"),
    ("count in period", "SELECT COUNT(*) c FROM {t} WHERE FDATE >= '2026-08-01' AND FDATE < '2026-10-01'"),
    ("select 10", "SELECT * FROM {t} LIMIT 10"),
]


def main() -> None:
    db = SessionLocal()
    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.name == "aipdp_data_warehouse_prod")
        .first()
    )
    if kb is None:
        print("KB not found")
        return

    from urllib.parse import quote_plus
    url = (
        f"mysql+pymysql://{quote_plus(kb.username or 'root')}:{quote_plus(kb.password or '')}"
        f"@{kb.host or 'localhost'}:{int(kb.port or 3306)}/{kb.database_name or ''}?charset=utf8mb4"
    )
    engine = acquire_engine("mysql", url, connect_args={"connect_timeout": 15})

    print(f"{'table':<32} {'probe':<16} {'seconds':>9}  result")
    for t in CANDIDATES:
        for label, sql in PROBES:
            stmt = text(sql.format(t=t)).execution_options(
                # No MAX_EXECUTION_TIME hint and a generous client-side budget: the
                # question is how long the view NEEDS, not whether the platform
                # would have allowed it.
            )
            t0 = time.perf_counter()
            try:
                with engine.connect() as c:
                    rows = c.execute(stmt).fetchall()
                dt = time.perf_counter() - t0
                head = str(rows[0]) if rows else "(no rows)"
                print(f"{t:<32} {label:<16} {dt:>9.2f}  {head[:60]}")
            except Exception as e:
                dt = time.perf_counter() - t0
                print(f"{t:<32} {label:<16} {dt:>9.2f}  ERR {type(e).__name__}: {str(e)[:70]}")


if __name__ == "__main__":
    main()
