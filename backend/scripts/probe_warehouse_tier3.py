#!/usr/bin/env python3
"""Probe the MySQL data warehouse for Tier 3 fundamental data.

Discovers ALL tables via SHOW TABLES, then probes:
  T3.1 Operating rate  → erp_v_sale_orderentry (produce_qty, plan_qty)
  T3.2 Inventory        → erp_v_stk_inventory (FBASEQTY), sale_erp_v_* (instock_qty)
  T3.3 Import prices    → sale_erp_v_* international supplier columns

Read-only. No writes.

Usage:
    docker exec zhanlu-backend python /app/scripts/probe_warehouse_tier3.py
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlparse, unquote

TIER3_PROBES = {
    "erp_v_stk_inventory": {
        "tier": "T3.2 Inventory",
        "probe_columns": ["FBASEQTY", "material_name", "FDATE", "biz_date"],
        "date_columns": ["FDATE", "biz_date", "FCREATEDATE", "FMODIFYDATE"],
    },
    "erp_v_sale_orderentry": {
        "tier": "T3.1 Operating Rate",
        "probe_columns": ["produce_qty", "plan_qty", "material_name", "FDATE", "biz_date"],
        "date_columns": ["FDATE", "biz_date", "FCREATEDATE", "FMODIFYDATE", "FPLANDELIVERYDATE"],
    },
}


def get_connection():
    """Connect using EDIA_MYSQL_URL from env."""
    url = os.environ.get("EDIA_MYSQL_URL", "")
    if not url:
        print("# EDIA_MYSQL_URL not set", file=sys.stderr)
        sys.exit(1)

    import pymysql
    parsed = urlparse(url)
    host = parsed.hostname or "10.10.10.49"
    port = parsed.port or 3306
    user = parsed.username or "root"
    password = unquote(parsed.password or "")
    db = parsed.path.lstrip("/") or "aipdp_data_warehouse_prod"

    print(f"# Connecting to {user}@{host}:{port}/{db}")
    conn = pymysql.connect(
        host=host, port=port, user=user, password=password,
        database=db, connect_timeout=10, charset="utf8mb4",
    )
    print("# Connected.\n")
    return conn


def show_all_tables(conn) -> list[str]:
    """Discover ALL tables in the warehouse."""
    with conn.cursor() as cur:
        cur.execute("SHOW TABLES")
        tables = [row[0] for row in cur.fetchall()]
    return tables


def describe_table(conn, table: str) -> list[dict]:
    """Get column info for a table."""
    with conn.cursor() as cur:
        cur.execute(f"SHOW FULL COLUMNS FROM `{table}`")
        cols = cur.fetchall()
    return [
        {"name": c[0], "type": c[1], "null": c[3], "comment": c[8] if len(c) > 8 else ""}
        for c in cols
    ]


def probe_table_basics(conn, table: str) -> dict:
    """Row count + date range for a table."""
    info: dict = {"table": table}
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM `{table}`")
            info["rows"] = cur.fetchone()[0]

            cols = describe_table(conn, table)
            col_names = [c["name"] for c in cols]
            info["columns"] = col_names

            # Try common date columns
            date_candidates = [
                "FDATE", "biz_date", "Biz_date", "FBizDate",
                "FCREATEDATE", "FMODIFYDATE", "fdate", "create_time",
                "created_at", "date", "dt",
            ]
            date_col = None
            for dc in date_candidates:
                if dc in col_names:
                    date_col = dc
                    break

            if not date_col:
                # Fuzzy match
                for cn in col_names:
                    cnl = cn.lower()
                    if "date" in cnl or "time" in cnl:
                        date_col = cn
                        break

            if info["rows"] > 0 and date_col:
                try:
                    cur.execute(
                        f"SELECT MIN(`{date_col}`), MAX(`{date_col}`) FROM `{table}`"
                    )
                    dr = cur.fetchone()
                    info["date_col"] = date_col
                    info["date_range"] = [
                        str(dr[0]) if dr and dr[0] else None,
                        str(dr[1]) if dr and dr[1] else None,
                    ]
                except Exception as e:
                    info["date_col"] = date_col
                    info["date_range_error"] = str(e)[:80]
            else:
                info["date_col"] = date_col or "-"
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {str(e)[:100]}"
    return info


def probe_column_stats(conn, table: str, column: str) -> dict:
    """Check if a specific column has non-null data."""
    stats: dict = {"column": column}
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(`{column}`) AS non_null, "
                f"COUNT(DISTINCT `{column}`) AS distinct_vals "
                f"FROM `{table}`"
            )
            row = cur.fetchone()
            stats["non_null"] = row[0] if row else 0
            stats["distinct"] = row[1] if row else 0

            # Sample values
            cur.execute(
                f"SELECT DISTINCT `{column}` FROM `{table}` "
                f"WHERE `{column}` IS NOT NULL LIMIT 5"
            )
            samples = [r[0] for r in cur.fetchall()]
            stats["samples"] = [str(s)[:50] for s in samples]
    except Exception as e:
        stats["error"] = str(e)[:80]
    return stats


def probe_sale_erp_tables(conn, tables: list[str]) -> list[dict]:
    """Probe sale_erp_v_* tables for instock_qty + international supplier columns."""
    results = []
    for table in tables:
        if not table.startswith("sale_erp_v_"):
            continue
        info: dict = {"table": table}
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM `{table}`")
                info["rows"] = cur.fetchone()[0]

                cols = describe_table(conn, table)
                col_names = [c["name"] for c in cols]
                info["total_columns"] = len(col_names)

                # Check instock_qty
                if "instock_qty" in col_names:
                    info["instock_qty"] = probe_column_stats(conn, table, "instock_qty")

                # Check date column
                for dc in ["biz_date", "FDATE", "Biz_date"]:
                    if dc in col_names:
                        try:
                            cur.execute(
                                f"SELECT MIN(`{dc}`), MAX(`{dc}`) FROM `{table}`"
                            )
                            dr = cur.fetchone()
                            info["date_col"] = dc
                            info["date_range"] = [
                                str(dr[0]) if dr and dr[0] else None,
                                str(dr[1]) if dr and dr[1] else None,
                            ]
                        except Exception:
                            pass
                        break

                # Find international supplier columns (import prices T3.3)
                intl_keywords = [
                    "ecisco", "arlanxeo", "kraton", "ineos", "kolon",
                    "lcy", "tribute", "import", "overseas", "foreign",
                    "international",
                ]
                intl_cols = []
                for c in cols:
                    cn_lower = (c["name"] or "").lower()
                    if any(kw in cn_lower for kw in intl_keywords):
                        intl_cols.append(c["name"])
                if intl_cols:
                    info["intl_supplier_cols"] = intl_cols
                    # Probe first 3
                    info["intl_samples"] = []
                    for ic in intl_cols[:3]:
                        info["intl_samples"].append(
                            probe_column_stats(conn, table, ic)
                        )

        except Exception as e:
            info["error"] = str(e)[:100]
        results.append(info)
    return results


def probe_tier3_tables(conn) -> list[dict]:
    """Deep-probe the specific Tier 3 tables."""
    results = []
    for table, spec in TIER3_PROBES.items():
        info: dict = {"table": table, "tier": spec["tier"]}
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM `{table}`")
                info["rows"] = cur.fetchone()[0]

                cols = describe_table(conn, table)
                col_names = [c["name"] for c in cols]
                info["all_columns"] = col_names

                # Date range
                for dc in spec["date_columns"]:
                    if dc in col_names:
                        try:
                            cur.execute(
                                f"SELECT MIN(`{dc}`), MAX(`{dc}`) FROM `{table}`"
                            )
                            dr = cur.fetchone()
                            info["date_col"] = dc
                            info["date_range"] = [
                                str(dr[0]) if dr and dr[0] else None,
                                str(dr[1]) if dr and dr[1] else None,
                            ]
                            break
                        except Exception:
                            continue

                # Probe specific columns
                info["column_stats"] = {}
                for pc in spec["probe_columns"]:
                    if pc in col_names:
                        info["column_stats"][pc] = probe_column_stats(
                            conn, table, pc
                        )

        except Exception as e:
            info["error"] = str(e)[:100]
        results.append(info)
    return results


def main() -> int:
    conn = get_connection()

    # ── Phase 1: Discover ALL tables ──
    print("=" * 100)
    print("PHASE 1: ALL TABLES IN WAREHOUSE")
    print("=" * 100)
    all_tables = show_all_tables(conn)
    print(f"Total tables/views: {len(all_tables)}\n")

    # Categorize
    categories: dict[str, list[str]] = {
        "erp_v_*": [],
        "sale_erp_v_*": [],
        "lz_v_*": [],
        "md_t_*": [],
        "v_*": [],
        "other": [],
    }
    for t in all_tables:
        if t.startswith("erp_v_"):
            categories["erp_v_*"].append(t)
        elif t.startswith("sale_erp_v_"):
            categories["sale_erp_v_*"].append(t)
        elif t.startswith("lz_v_"):
            categories["lz_v_*"].append(t)
        elif t.startswith("md_t_"):
            categories["md_t_*"].append(t)
        elif t.startswith("v_"):
            categories["v_*"].append(t)
        else:
            categories["other"].append(t)

    for cat, tbls in categories.items():
        print(f"\n  [{cat}] ({len(tbls)} tables)")
        for t in tbls:
            print(f"    {t}")

    # ── Phase 2: Basic probe of ALL tables (rows + date range) ──
    print("\n\n" + "=" * 100)
    print("PHASE 2: BASIC PROBE (rows + date range)")
    print("=" * 100)
    print(f"\n{'TABLE':<50} {'ROWS':>8}  {'DATE_COL':<20}  {'DATE_RANGE'}")
    print("-" * 110)
    for table in all_tables:
        info = probe_table_basics(conn, table)
        if "error" in info:
            print(f"{table:<50} {'ERR':>8}  {info['error'][:60]}")
            continue
        rows = info.get("rows", 0)
        date_col = info.get("date_col", "-") or "-"
        dr = info.get("date_range", ["-", "-"])
        dr_str = f"{dr[0]} → {dr[1]}" if dr != ["-", "-"] else "-"
        print(f"{table:<50} {rows:>8}  {date_col:<20}  {dr_str}")

    # ── Phase 3: Deep probe Tier 3 tables ──
    print("\n\n" + "=" * 100)
    print("PHASE 3: TIER 3 DEEP PROBE")
    print("=" * 100)
    tier3_results = probe_tier3_tables(conn)
    for info in tier3_results:
        print(f"\n── {info['table']} ({info['tier']}) ──")
        if "error" in info:
            print(f"  ERROR: {info['error']}")
            continue
        print(f"  Rows: {info.get('rows', 0)}")
        print(f"  Date col: {info.get('date_col', '-')}  Range: {info.get('date_range', '-')}")
        print(f"  All columns ({len(info.get('all_columns', []))}): {info.get('all_columns', [])}")
        if "column_stats" in info:
            print("  Column stats:")
            for col, stats in info["column_stats"].items():
                if "error" in stats:
                    print(f"    {col}: ERROR {stats['error']}")
                else:
                    print(
                        f"    {col}: non_null={stats['non_null']}, "
                        f"distinct={stats['distinct']}, "
                        f"samples={stats['samples']}"
                    )

    # ── Phase 4: Probe sale_erp_v_* tables for instock_qty + intl suppliers ──
    print("\n\n" + "=" * 100)
    print("PHASE 4: sale_erp_v_* PROBE (instock_qty + import suppliers)")
    print("=" * 100)
    sale_erp_tables = [t for t in all_tables if t.startswith("sale_erp_v_")]
    sale_results = probe_sale_erp_tables(conn, sale_erp_tables)
    for info in sale_results:
        print(f"\n── {info['table']} ──")
        if "error" in info:
            print(f"  ERROR: {info['error']}")
            continue
        print(f"  Rows: {info.get('rows', 0)}  Columns: {info.get('total_columns', 0)}")
        print(f"  Date: {info.get('date_col', '-')}  Range: {info.get('date_range', '-')}")
        if "instock_qty" in info:
            isq = info["instock_qty"]
            print(
                f"  instock_qty: non_null={isq.get('non_null', 0)}, "
                f"distinct={isq.get('distinct', 0)}, "
                f"samples={isq.get('samples', [])}"
            )
        else:
            print("  instock_qty: COLUMN NOT FOUND")
        if "intl_supplier_cols" in info:
            print(f"  International supplier columns ({len(info['intl_supplier_cols'])}):")
            for ic in info["intl_supplier_cols"]:
                print(f"    - {ic}")
            if "intl_samples" in info:
                for s in info["intl_samples"]:
                    print(
                        f"    [{s['column']}]: non_null={s.get('non_null', 0)}, "
                        f"distinct={s.get('distinct', 0)}, "
                        f"samples={s.get('samples', [])}"
                    )
        else:
            print("  International supplier columns: NONE FOUND")

    conn.close()
    print("\n# Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
