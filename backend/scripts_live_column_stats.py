#!/usr/bin/env python3
"""Live proof: catalog column statistics (slice 2) against real databases.

Runs the PLATFORM's own connector + the real statistics pass — no test doubles —
so it catches the class of defect a canned rows test cannot (the slice-1 run is
what found the PostgreSQL index query reading ``indisunique`` off the wrong
catalog row).

Credentials come from the environment, never from this file:

    LIVE_MYSQL_HOST / _PORT / _DB / _USER / _PASSWORD
    LIVE_PG_HOST / _PORT / _DB / _USER / _PASSWORD

Usage (from the repo root, backend venv on sys.path):

    python backend/scripts_live_column_stats.py mysql erp_v_sale_orderentry
    python backend/scripts_live_column_stats.py pg customers orders
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from app.services.db.connector_factory import get_connector  # noqa: E402
from app.services.knowledge_graph import catalog_indexer as cat  # noqa: E402
from app.services.knowledge_graph import column_stats as cs  # noqa: E402


def _kb(kind: str):
    prefix = {"mysql": "LIVE_MYSQL", "mariadb": "LIVE_MARIADB", "pg": "LIVE_PG"}[kind]
    db_type = {"mysql": "mysql", "mariadb": "mariadb", "pg": "postgresql"}[kind]
    return SimpleNamespace(
        id=f"live-{kind}",
        db_type=db_type,
        host=os.environ[f"{prefix}_HOST"],
        port=int(os.environ.get(f"{prefix}_PORT") or (3306 if kind != "pg" else 5432)),
        database_name=os.environ[f"{prefix}_DB"],
        username=os.environ[f"{prefix}_USER"],
        password=os.environ.get(f"{prefix}_PASSWORD") or "",
        api_url="",
        file_url="",
    )


def main() -> int:
    kind = sys.argv[1]
    tables = sys.argv[2:]
    kb = _kb(kind)
    with get_connector(kb) as conn:
        for table in tables:
            print(f"\n=== {table} ({kind}) ===")
            columns = conn.describe_table(table)
            norm = []
            for c in columns:
                norm.append(
                    {
                        "column_name": c.get("name") or c.get("column_name"),
                        "data_type": c.get("type") or c.get("data_type") or "",
                        "is_primary_key": bool(c.get("pk")),
                        "is_nullable": bool(c.get("nullable", True)),
                        "comment": c.get("comment"),
                    }
                )
            exact = cat._safe_row_count(conn, table)
            facts = cat._safe_table_facts(conn, table)
            est = facts.get("est_rows")
            print(f"  columns={len(norm)} exact_count={exact} est_rows={est} facts={facts}")

            profile = cs.profile_table_columns(conn, table, norm) if (exact or est) else {}
            print(f"  profiled columns: {len(profile)}/{len(norm)}")

            for c in norm:
                name = c["column_name"]
                s = profile.get(name)
                if s is None:
                    print(f"    - {name:<28} {c['data_type']:<16} (no stats)")
                    continue
                top = ", ".join(
                    f"{v['value']}×{v['count']}" for v in (s.get("top_values") or [])[:5]
                )
                print(
                    f"    - {name:<28} {c['data_type']:<16} basis={s['basis']:<6} "
                    f"rows={s['rows_sampled']:<4} nulls={s['null_rate']:.0%} "
                    f"distinct={s['distinct']}{'' if s['distinct_is_exact'] else '≥'}"
                    + (f" range={s.get('min')}…{s.get('max')}" if s.get("min") is not None else "")
                    + (f" | {top}" if top else "")
                )

            # The two downstream shapes, exactly as the indexer persists them.
            col_values = {
                n: cs.column_sample_values(profile.get(n)) for n in profile
            }
            persisted = {k: v for k, v in col_values.items() if v}
            print(f"  kb_column_meta.sample_values → {len(persisted)} columns")
            print(f"  kb_table_meta.sample_values  → {json.dumps(cs.table_sample_values(profile), ensure_ascii=False)[:300]}")
            sample_col = next(iter(persisted), None)
            if sample_col:
                print(f"  prompt line example:\n    "
                      + cat._column_prompt_line(
                          next(c for c in norm if c["column_name"] == sample_col)
                          | {"stats": profile.get(sample_col)}
                      ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
