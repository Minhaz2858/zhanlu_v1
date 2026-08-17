#!/usr/bin/env python3
"""Verify that `material_model` and `F_PAEZ_JC` hold identical data.

One-time safety check before steering the ecisco_bi_assistant to prefer
`F_PAEZ_JC` over the deprecated `material_model` column. Discovers every
table in the EDIA warehouse that exposes BOTH columns, then diffs them
(NULL-safe) and reports parity.

Read-only. No writes.

Usage:
    docker exec zhanlu-backend python /app/scripts/verify_f_paez_jc_parity.py
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlparse, unquote

# Fallback when EDIA_MYSQL_URL is not in the environment. Matches the
# example in backend/app/config.py.
_DEFAULT_URL = (
    "mysql+pymysql://root:0Gg.B7c2%40tcX_jne6FMh@10.10.10.49:3306/"
    "aipdp_data_warehouse_prod"
)

# Tables known to hold a material-grade / material-model pair. Left empty to
# auto-discover via information_schema (recommended); populate to restrict
# the check to specific tables.
ONLY_TABLES: list[str] = []


def get_connection():
    url = os.environ.get("EDIA_MYSQL_URL") or _DEFAULT_URL
    import pymysql

    parsed = urlparse(url)
    host = parsed.hostname or "10.10.10.49"
    port = parsed.port or 3306
    user = parsed.username or "root"
    password = unquote(parsed.password or "")
    db = parsed.path.lstrip("/") or "aipdp_data_warehouse_prod"

    print(f"# Connecting to {user}@{host}:{port}/{db}")
    conn = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=db,
        connect_timeout=10,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    print("# Connected.\n")
    return conn


def find_tables_with_both(conn) -> list[str]:
    """Return tables exposing both `material_model` and `F_PAEZ_JC`."""
    db = conn.db.decode() if isinstance(conn.db, bytes) else conn.db
    if ONLY_TABLES:
        return list(ONLY_TABLES)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT TABLE_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s
              AND COLUMN_NAME IN ('material_model', 'F_PAEZ_JC')
            GROUP BY TABLE_NAME
            HAVING COUNT(DISTINCT COLUMN_NAME) = 2
            ORDER BY TABLE_NAME
            """,
            (db,),
        )
        return [row["TABLE_NAME"] for row in cur.fetchall()]


def check_parity(conn, table: str) -> dict:
    """Diff material_model vs F_PAEZ_JC (NULL-safe) for one table."""
    sql = (
        f"SELECT COUNT(*) AS total, "
        f"SUM(NOT (material_model <=> F_PAEZ_JC)) AS mismatches "
        f"FROM `{table}`"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    total = int(row["total"] or 0)
    mismatches = int(row["mismatches"] or 0)
    return {"total": total, "mismatches": mismatches}


def main() -> int:
    conn = get_connection()
    try:
        tables = find_tables_with_both(conn)
        if not tables:
            print("# No tables found with both `material_model` and `F_PAEZ_JC`.")
            print("# Nothing to migrate — the columns may already be renamed, or")
            print("# F_PAEZ_JC lives only in a subset of tables.")
            return 0

        print(f"# Found {len(tables)} table(s) with both columns.\n")
        all_ok = True
        for table in tables:
            result = check_parity(conn, table)
            ok = result["mismatches"] == 0
            all_ok = all_ok and ok
            status = "OK" if ok else "MISMATCH"
            print(
                f"  [{status}] {table}: "
                f"total={result['total']} mismatches={result['mismatches']}"
            )

        print()
        if all_ok:
            print("# PARITY CONFIRMED: material_model and F_PAEZ_JC are identical")
            print("# in every table that has both. Safe to prefer F_PAEZ_JC.")
            return 0
        print("# PARITY BROKEN: some rows differ between the two columns.")
        print("# Investigate before deprecating material_model.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
