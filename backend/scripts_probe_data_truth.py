"""Ground-truth probe: where does the warehouse ACTUALLY have recent data?

Motivating symptom (live chat_messages, 2026-09-18): the agent answered
"erp_product_sales_details 的 shipment_date 只覆盖 2025 年全年" and
"合同执行数据查不到任何可汇总记录" for questions about 2026-08/2026-09.

This asks the warehouse itself, read-only, on every table that has a
recognisable date column: row count, date span, and the count inside the period
the user actually asked about. Output is sorted so the tables that DO carry
in-period rows come first — that set is what the answering path should have
picked.

Run inside the container:

    docker exec zhanlu-copy-backend sh -lc 'cd /app && python scripts_probe_data_truth.py > /tmp/dt.out 2>/dev/null'
"""
from __future__ import annotations

from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.services.db.connector_factory import get_connector

PERIOD_START = "2026-08-01"
PERIOD_END = "2026-10-01"
DATE_COLS = ("shipment_date", "sale_date", "FDATE", "FDate", "order_date",
             "delivery_date", "created_date", "date")


def main() -> None:
    db = SessionLocal()
    kbs = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.source_kind == "database",
                KnowledgeBase.is_deleted == False)  # noqa: E712
        .all()
    )
    for kb in kbs:
        if kb.db_type != "mysql":
            continue
        print("=" * 104)
        print(f"KB {kb.name} ({kb.db_type} {kb.host}/{kb.database_name})")
        with get_connector(kb) as conn:
            names = list(conn.list_tables())
            print(f"tables: {len(names)}")

            hits = []
            for t in names:
                try:
                    cols = {c["name"] for c in conn.describe_table(t)}
                except Exception as e:
                    print(f"  ! describe {t}: {type(e).__name__}: {e}")
                    continue
                for dc in DATE_COLS:
                    if dc in cols:
                        hits.append((t, dc))
                        break
            print(f"tables with a recognisable date column: {len(hits)}")

            print(f"\n{'table':<44} {'date col':<16} {'rows':>9} {'min':<12} {'max':<12} {'in-period':>10}")
            rows_out = []
            errs = 0
            for t, dc in hits:
                try:
                    r = conn.execute(f"SELECT COUNT(*) n, MIN({dc}) mn, MAX({dc}) mx FROM {t}")[0]
                    n, mn, mx = r["n"], r["mn"], r["mx"]
                except Exception as e:
                    errs += 1
                    print(f"  ! {t}.{dc}: {type(e).__name__}: {str(e)[:110]}")
                    continue
                try:
                    ip = conn.execute(
                        f"SELECT COUNT(*) c FROM {t} WHERE {dc} >= '{PERIOD_START}' AND {dc} < '{PERIOD_END}'"
                    )[0]["c"]
                except Exception:
                    ip = -1
                rows_out.append((t, dc, n, str(mn), str(mx), ip))
            rows_out.sort(key=lambda x: -x[5])
            for t, dc, n, mn, mx, ip in rows_out:
                print(f"{t[:44]:<44} {dc:<16} {n:>9} {mn[:12]:<12} {mx[:12]:<12} {ip:>10}")

            print(f"\nerrors: {errs}")
            print("tables the agent SHOULD have used (in-period rows > 0):")
            for t, dc, n, mn, mx, ip in rows_out:
                if ip > 0:
                    print(f"  {t:<44} [{dc}]  in-period={ip:>7}  span={mn[:10]}..{mx[:10]}")


if __name__ == "__main__":
    main()
