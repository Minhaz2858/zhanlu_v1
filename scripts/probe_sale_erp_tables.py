#!/usr/bin/env python3
"""Discover all sale_erp_v_* tables and their material/product coverage.

For each table:
  - Row count (total + FTAXPRICE > 0)
  - Date range (PLANDATE or equivalent)
  - Distinct material_code values with row counts
  - Sample rows

Also probes erp_v_sale_huizhou as a potential universal source.
"""
from __future__ import annotations

import os
import sys
from urllib.parse import urlparse, unquote

import pymysql


def get_connection():
    url = os.environ.get("EDIA_MYSQL_URL", "")
    if not url:
        print("# EDIA_MYSQL_URL not set", file=sys.stderr)
        sys.exit(1)
    parsed = urlparse(url)
    conn = pymysql.connect(
        host=parsed.hostname or "10.10.10.49",
        port=parsed.port or 3306,
        user=parsed.username or "root",
        password=unquote(parsed.password or ""),
        database=(parsed.path.lstrip("/") or "aipdp_data_warehouse_prod"),
        connect_timeout=10, charset="utf8mb4",
    )
    return conn


def list_sale_erp_tables(conn):
    """List all tables matching sale_erp_v_%"""
    with conn.cursor() as cur:
        cur.execute("SHOW TABLES LIKE 'sale_erp\\_v\\_%'")
        return [r[0] for r in cur.fetchall()]


def probe_sale_erp_table(conn, table):
    """Deep-probe a single sale_erp_v_* table."""
    print(f"\n{'='*100}")
    print(f"TABLE: {table}")
    print(f"{'='*100}")

    try:
        with conn.cursor() as cur:
            # Total row count
            cur.execute(f"SELECT COUNT(*) FROM `{table}`")
            total = cur.fetchone()[0]
            print(f"  Total rows: {total}")

            # Get columns
            cur.execute(f"SHOW FULL COLUMNS FROM `{table}`")
            cols_raw = cur.fetchall()
            col_names = [c[0] for c in cols_raw]
            print(f"  Columns ({len(col_names)}): {col_names}")

            # Check which key columns exist
            has_ftaxprice = "FTAXPRICE" in col_names
            has_material_code = "material_code" in col_names
            has_plandate = "PLANDATE" in col_names

            # FTAXPRICE coverage
            if has_ftaxprice:
                cur.execute(
                    f"SELECT COUNT(*) FROM `{table}` WHERE FTAXPRICE > 0"
                )
                nonzero = cur.fetchone()[0]
                print(f"  FTAXPRICE > 0: {nonzero} rows ({nonzero*100//total if total else 0}%)")

            # Date range
            date_col = "PLANDATE" if has_plandate else None
            if not date_col:
                for dc in col_names:
                    if any(k in dc.lower() for k in ["date", "time", "fdate"]):
                        date_col = dc
                        break
            if date_col:
                cur.execute(
                    f"SELECT MIN(`{date_col}`), MAX(`{date_col}`) FROM `{table}`"
                )
                dr = cur.fetchone()
                print(f"  Date range [{date_col}]: {dr[0]} → {dr[1]}")

            # Material codes with row counts
            if has_material_code and has_ftaxprice:
                cur.execute(
                    f"SELECT material_code, COUNT(*) AS cnt, "
                    f"MIN(`{date_col}`) AS first_date, MAX(`{date_col}`) AS last_date "
                    f"FROM `{table}` "
                    f"WHERE FTAXPRICE > 0 AND material_code IS NOT NULL "
                    f"AND material_code != '' "
                    f"GROUP BY material_code "
                    f"ORDER BY cnt DESC"
                )
                materials = cur.fetchall()
                print(f"\n  Material codes ({len(materials)}):")
                for code, cnt, first_d, last_d in materials:
                    flag = " *** FORECASTABLE" if cnt >= 90 else (" * marginal" if cnt >= 50 else "")
                    print(f"    {code:<40} rows={cnt:>5}  {first_d} → {last_d}{flag}")

                # Price range per material
                print(f"\n  Price ranges per material_code:")
                for code, cnt, _, _ in materials[:10]:  # top 10
                    cur.execute(
                        f"SELECT MIN(FTAXPRICE), MAX(FTAXPRICE), "
                        f"ROUND(AVG(FTAXPRICE),2) "
                        f"FROM `{table}` "
                        f"WHERE material_code = '{code}' AND FTAXPRICE > 0"
                    )
                    pr = cur.fetchone()
                    print(f"    {code:<40} min={pr[0]:>10}  max={pr[1]:>10}  avg={pr[2]:>10}")

            # Sample 2 rows
            print(f"\n  Sample rows:")
            cur.execute(f"SELECT * FROM `{table}` LIMIT 2")
            for i, row in enumerate(cur.fetchall()):
                print(f"    Row {i}:")
                for cn, val in zip(col_names, row):
                    if val is not None:
                        print(f"      {cn}: {str(val)[:60]}")

    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")


def probe_huizhou(conn):
    """Probe erp_v_sale_huizhou — 13,912 rows, potential universal source."""
    table = "erp_v_sale_huizhou"
    print(f"\n{'='*100}")
    print(f"TABLE: {table} (universal source candidate)")
    print(f"{'='*100}")

    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM `{table}`")
            total = cur.fetchone()[0]
            print(f"  Total rows: {total}")

            cur.execute(f"SHOW FULL COLUMNS FROM `{table}`")
            cols_raw = cur.fetchall()
            col_names = [c[0] for c in cols_raw]
            print(f"  Columns ({len(col_names)}):")
            for c in cols_raw:
                comment = f"  -- {c[8]}" if len(c) > 8 and c[8] else ""
                print(f"    {c[0]:<35} {c[1]:<30}{comment}")

            # Find date column
            date_col = None
            for dc in col_names:
                if any(k in dc.lower() for k in ["date", "time", "fdate", "plandate"]):
                    date_col = dc
                    break
            if date_col:
                cur.execute(
                    f"SELECT MIN(`{date_col}`), MAX(`{date_col}`) FROM `{table}`"
                )
                dr = cur.fetchone()
                print(f"\n  Date range [{date_col}]: {dr[0]} → {dr[1]}")

            # Find price column
            price_col = None
            for pc in col_names:
                if any(k in pc.lower() for k in ["price", "taxprice", "fprice", "ftaxprice"]):
                    price_col = pc
                    break
            if price_col:
                cur.execute(f"SELECT COUNT(*) FROM `{table}` WHERE `{price_col}` > 0")
                nz = cur.fetchone()[0]
                print(f"  {price_col} > 0: {nz} rows ({nz*100//total if total else 0}%)")

            # Find material/product column
            mat_col = None
            for mc in col_names:
                if any(k in mc.lower() for k in ["material", "product", "fname", "item"]):
                    mat_col = mc
                    break

            if mat_col and price_col and date_col:
                cur.execute(
                    f"SELECT `{mat_col}`, COUNT(*) AS cnt, "
                    f"MIN(`{date_col}`), MAX(`{date_col}`) "
                    f"FROM `{table}` "
                    f"WHERE `{price_col}` > 0 AND `{mat_col}` IS NOT NULL "
                    f"GROUP BY `{mat_col}` "
                    f"ORDER BY cnt DESC"
                )
                materials = cur.fetchall()
                print(f"\n  Materials ({len(materials)}):")
                for name, cnt, first_d, last_d in materials:
                    flag = " *** FORECASTABLE" if cnt >= 90 else (" * marginal" if cnt >= 50 else "")
                    print(f"    {str(name)[:40]:<40} rows={cnt:>5}  {first_d} → {last_d}{flag}")

                # Price range per material
                print(f"\n  Price ranges per material:")
                for name, cnt, _, _ in materials[:15]:
                    cur.execute(
                        f"SELECT MIN(`{price_col}`), MAX(`{price_col}`), "
                        f"ROUND(AVG(`{price_col}`),2) "
                        f"FROM `{table}` "
                        f"WHERE `{mat_col}` = %s AND `{price_col}` > 0",
                        (name,)
                    )
                    pr = cur.fetchone()
                    print(f"    {str(name)[:40]:<40} min={pr[0]:>10}  max={pr[1]:>10}  avg={pr[2]:>10}")

            # Sample 3 rows
            print(f"\n  Sample rows:")
            cur.execute(f"SELECT * FROM `{table}` LIMIT 3")
            for i, row in enumerate(cur.fetchall()):
                print(f"    Row {i}:")
                for cn, val in zip(col_names, row):
                    if val is not None:
                        print(f"      {cn}: {str(val)[:60]}")

    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")


def main():
    conn = get_connection()
    print("# Connected to warehouse.\n")

    # 1. List all sale_erp_v_* tables
    tables = list_sale_erp_tables(conn)
    print(f"# Found {len(tables)} sale_erp_v_* tables:")
    for t in tables:
        print(f"  - {t}")

    # 2. Probe each
    for t in tables:
        probe_sale_erp_table(conn, t)

    # 3. Probe erp_v_sale_huizhou
    probe_huizhou(conn)

    conn.close()
    print("\n# Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
