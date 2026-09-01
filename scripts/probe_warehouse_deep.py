#!/usr/bin/env python3
"""Deep-probe the most promising new warehouse tables found in Phase 1.

Focuses on tables with 8 years of time-series data that could serve as
Tier 3 proxies:
  - erp_t_stk_instock / instockentry   (T3.2 inventory movements)
  - erp_t_sal_outstock / outstockentry (T3.1 throughput / T3.2 inventory)
  - erp_t_sp_instock / instockentry    (T3.3 import purchase prices)
  - erp_t_stk_inventory                (T3.2 inventory snapshot, 50K rows)
  - erp_v_raw_material_receiving       (T3.3 raw material import)
  - erp_t_crm_contractentry            (T3.3 contract delivery prices)
  - oilchem_data                       (T3.1/T3.2 market data from 隆众)
  - erp_product_sales_details          (sales volume with shipment_date)
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlparse, unquote

DEEP_PROBE_TABLES = [
    "erp_t_stk_instock",
    "erp_t_stk_instockentry",
    "erp_t_sal_outstock",
    "erp_t_sal_outstockentry",
    "erp_t_sp_instock",
    "erp_t_sp_instockentry",
    "erp_t_stk_inventory",
    "erp_v_raw_material_receiving",
    "erp_t_crm_contractentry",
    "erp_product_sales_details",
    "oilchem_data",
    "erp_t_bd_supplier",
    "erp_v_sale_guangdong",
    "erp_v_sale_huizhou",
    "erp_v_sale_orderentry",
]


def get_connection():
    url = os.environ.get("EDIA_MYSQL_URL", "")
    if not url:
        print("# EDIA_MYSQL_URL not set", file=sys.stderr)
        sys.exit(1)
    import pymysql
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


def describe_table(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"SHOW FULL COLUMNS FROM `{table}`")
        return [
            {"name": c[0], "type": c[1], "null": c[3],
             "comment": c[8] if len(c) > 8 else ""}
            for c in cur.fetchall()
        ]


def probe_table(conn, table):
    print(f"\n{'='*100}")
    print(f"TABLE: {table}")
    print(f"{'='*100}")

    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM `{table}`")
            total = cur.fetchone()[0]
            print(f"Total rows: {total}")

            cols = describe_table(conn, table)
            print(f"Columns ({len(cols)}):")
            for c in cols:
                comment = f"  -- {c['comment']}" if c['comment'] else ""
                print(f"  {c['name']:<35} {c['type']:<25} null={c['null']}{comment}")

            col_names = [c["name"] for c in cols]

            # Find date columns
            date_cols = [c for c in col_names if
                         any(d in c.lower() for d in ["date", "time", "fdate", "plandate"])]

            # For each date column, get range
            for dc in date_cols[:3]:
                try:
                    cur.execute(f"SELECT MIN(`{dc}`), MAX(`{dc}`) FROM `{table}`")
                    dr = cur.fetchone()
                    print(f"\n  Date range [{dc}]: {dr[0]} → {dr[1]}")
                except Exception as e:
                    print(f"\n  Date range [{dc}]: ERROR {e}")

            # Find quantity/price-like columns and sample them
            qty_keywords = ["qty", "amount", "price", "fbaseqty", "fdelity",
                            "fproduceqty", "fqty", "fprice", "ftaxprice",
                            "famount", "produce_qty", "plan_qty", "instock_qty",
                            "freal", "foutqty", "fonhandqty"]
            qty_cols = [c for c in col_names if
                        any(k in c.lower() for k in qty_keywords)]

            if qty_cols:
                print(f"\n  Quantity/Price columns ({len(qty_cols)}):")
                for qc in qty_cols:
                    try:
                        cur.execute(
                            f"SELECT COUNT(`{qc}`) AS nn, "
                            f"COUNT(DISTINCT `{qc}`) AS dv, "
                            f"MIN(`{qc}`) AS mn, MAX(`{qc}`) AS mx "
                            f"FROM `{table}`"
                        )
                        row = cur.fetchone()
                        print(f"    {qc:<30} non_null={row[0]:>6}  distinct={row[1]:>6}  "
                              f"min={row[2]}  max={row[3]}")
                    except Exception as e:
                        print(f"    {qc:<30} ERROR: {str(e)[:60]}")

            # Find material/product columns
            mat_keywords = ["material", "product", "cust", "supplier", "stock",
                            "org", "billno", "entryid"]
            mat_cols = [c for c in col_names if
                        any(k in c.lower() for k in mat_keywords)]

            if mat_cols:
                print(f"\n  Dimension columns ({len(mat_cols)}):")
                for mc in mat_cols[:8]:
                    try:
                        cur.execute(
                            f"SELECT COUNT(DISTINCT `{mc}`) AS dv "
                            f"FROM `{table}`"
                        )
                        dv = cur.fetchone()[0]
                        cur.execute(
                            f"SELECT DISTINCT `{mc}` FROM `{table}` "
                            f"WHERE `{mc}` IS NOT NULL LIMIT 5"
                        )
                        samples = [str(r[0])[:40] for r in cur.fetchall()]
                        print(f"    {mc:<30} distinct={dv:>5}  samples={samples}")
                    except Exception as e:
                        print(f"    {mc:<30} ERROR: {str(e)[:60]}")

            # Sample 3 rows
            print(f"\n  Sample rows:")
            cur.execute(f"SELECT * FROM `{table}` LIMIT 3")
            sample_rows = cur.fetchall()
            col_names_tuple = tuple(col_names)
            for i, row in enumerate(sample_rows):
                print(f"    Row {i}:")
                for cn, val in zip(col_names_tuple, row):
                    if val is not None:
                        print(f"      {cn}: {str(val)[:60]}")

    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")


def probe_supplier_imports(conn):
    """Check erp_t_bd_supplier for international suppliers."""
    print(f"\n{'='*100}")
    print("SUPPLIER ANALYSIS: Looking for international/import suppliers")
    print(f"{'='*100}")
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW FULL COLUMNS FROM `erp_t_bd_supplier`")
            cols = cur.fetchall()
            col_names = [c[0] for c in cols]
            print(f"Columns: {col_names}")

            # Find name-like columns
            name_col = None
            for cn in col_names:
                if "name" in cn.lower() or "fname" in cn.lower():
                    name_col = cn
                    break

            if name_col:
                cur.execute(f"SELECT COUNT(*) FROM `erp_t_bd_supplier`")
                total = cur.fetchone()[0]
                print(f"\nTotal suppliers: {total}")

                # Sample all suppliers (only 2211 rows)
                cur.execute(f"SELECT `{name_col}` FROM `erp_t_bd_supplier` "
                            f"WHERE `{name_col}` IS NOT NULL")
                all_names = [r[0] for r in cur.fetchall()]

                # International keywords
                intl_kw = ["llc", "ltd", "inc", "gmbh", "corp", "co.", "sa",
                           "bv", "ag", "international", "global", "trading",
                           "energy", "polymers", "chemical", "ineos", "kraton",
                           "arlanxeo", "kolon", "lcy", "tribute", "ecisco",
                           "sinopec", "import", "overseas", "korea", "japan",
                           "usa", "germany", "singapore", "thailand"]
                intl_suppliers = []
                for name in all_names:
                    if name and any(kw in (name or "").lower() for kw in intl_kw):
                        intl_suppliers.append(name)

                print(f"\nPotential international/import suppliers ({len(intl_suppliers)}):")
                for s in sorted(set(intl_suppliers)):
                    print(f"  - {s}")

                # Also show all distinct supplier names (might be manageable)
                if total <= 3000:
                    print(f"\nAll distinct supplier names ({len(set(all_names))}):")
                    for s in sorted(set(all_names)):
                        print(f"  - {s}")

    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")


def probe_oilchem(conn):
    """Deep probe oilchem_data — this is 隆众 (Longzhong) market data!"""
    print(f"\n{'='*100}")
    print("OILCHEM DATA (隆众资讯 market data)")
    print(f"{'='*100}")
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW FULL COLUMNS FROM `oilchem_data`")
            cols = cur.fetchall()
            print("Columns:")
            for c in cols:
                comment = f"  -- {c[8]}" if len(c) > 8 and c[8] else ""
                print(f"  {c[0]:<30} {c[1]}{comment}")

            cur.execute("SELECT * FROM `oilchem_data` LIMIT 10")
            rows = cur.fetchall()
            col_names = [c[0] for c in cols]
            print(f"\nSample rows ({len(rows)}):")
            for i, row in enumerate(rows):
                print(f"  Row {i}:")
                for cn, val in zip(col_names, row):
                    if val is not None:
                        print(f"    {cn}: {str(val)[:80]}")
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")


def main():
    conn = get_connection()
    print("# Connected to warehouse.\n")

    for table in DEEP_PROBE_TABLES:
        probe_table(conn, table)

    probe_supplier_imports(conn)
    probe_oilchem(conn)

    conn.close()
    print("\n# Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
