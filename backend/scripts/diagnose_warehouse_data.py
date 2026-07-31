#!/usr/bin/env python3
"""Diagnose the MySQL warehouse to find forecastable time-series data.

Reads KB config from zhanlu's Postgres `knowledge_bases` table, then probes
candidate warehouse tables for row counts, date ranges, and column schemas.
Read-only; no writes.

Usage:
    # From the running zhanlu-backend docker container (pymysql is installed):
    docker exec zhanlu-backend python /path/to/diagnose_warehouse_data.py

    # Or locally if pymysql is in venv:
    PYTHONPATH=. venv/bin/python scripts/diagnose_warehouse_data.py
"""

from __future__ import annotations

import subprocess
import sys
import json
from typing import Optional

# Candidate tables per spec §7
CANDIDATE_TABLES = [
    "actual_price",
    "v_actual_price",
    "v_lz_data",
    "v_forecast_price",
    "v_partner_forecast_price",
    "v_market_data",
    "monthly_prices",
    "lz_v_sis_data",
    "lz_v_异戊二烯_data",
    "lz_v_苯乙烯_data",
    "lz_v_裂解c5_data",
    "lz_v_裂解c9_data",
    "lz_v_双环戊二烯_data",
    "lz_v_戊烷泡发剂_data",
    "lz_v_混三甲苯_data",
    "lz_v_环戊烷_data",
    "lz_v_甲基四氢苯酐_data",
    "lz_v_正戊烷_data",
    "lz_v_异戊烷_data",
    "lz_v_间戊二烯_data",
    "market_prices",
    "forecast_price",
]


def get_kb_config_via_docker() -> Optional[dict]:
    """Try to read the warehouse KB config from zhanlu-postgres via docker."""
    sql = (
        "SELECT id, host, port, database_name, username, password "
        "FROM knowledge_bases "
        "WHERE source_kind='database' AND db_type='mysql' "
        "AND database_name='aipdp_data_warehouse_prod' "
        "AND is_deleted=false LIMIT 1;"
    )
    cmd = [
        "docker", "exec", "zhanlu-postgres", "psql",
        "-U", "zhanlu", "-d", "zhanlu", "-t", "-A", "-F", "|", "-c", sql,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception as e:
        print(f"# docker psql failed: {e}", file=sys.stderr)
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    parts = [p.strip() for p in out.stdout.strip().splitlines()[0].split("|")]
    if len(parts) < 6:
        return None
    return {
        "kb_id": parts[0],
        "host": parts[1],
        "port": int(parts[2] or "3306"),
        "database_name": parts[3],
        "username": parts[4],
        "password": parts[5],
    }


def get_kb_config_via_env() -> Optional[dict]:
    """Fallback: read E2E_MYSQL_* env vars (set in CI)."""
    import os
    if not os.environ.get("E2E_MYSQL_HOST"):
        return None
    return {
        "kb_id": None,
        "host": os.environ["E2E_MYSQL_HOST"],
        "port": int(os.environ.get("E2E_MYSQL_PORT", "3306")),
        "database_name": os.environ["E2E_MYSQL_DB"],
        "username": os.environ["E2E_MYSQL_USER"],
        "password": os.environ["E2E_MYSQL_PASS"],
    }


def diagnose_table(conn, table: str) -> dict:
    """Probe a single table: row count, columns, date range."""
    info: dict = {"table": table}
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM `{table}`")
            info["rows"] = cur.fetchone()[0]

            cur.execute(f"SHOW FULL COLUMNS FROM `{table}`")
            cols = cur.fetchall()
            info["columns"] = [
                {"name": c[0], "type": c[1], "comment": c[8]} for c in cols
            ]

            # Find first date-like column
            date_col = None
            for c in cols:
                cn = (c[0] or "").lower()
                ct = (c[1] or "").lower()
                if "date" in cn or "time" in cn or "date" in ct or "datetime" in ct:
                    date_col = c[0]
                    break

            if info["rows"] > 0 and date_col:
                # Quote the column name (handles weird names like 'biz_date(业务日期)')
                quoted = f"`{date_col}`"
                try:
                    cur.execute(
                        f"SELECT MIN({quoted}), MAX({quoted}) FROM `{table}`"
                    )
                    dr = cur.fetchone()
                    info["date_col"] = date_col
                    info["date_range"] = [
                        str(dr[0]) if dr and dr[0] else None,
                        str(dr[1]) if dr and dr[1] else None,
                    ]
                except Exception as e:
                    info["date_col"] = date_col
                    info["date_range_error"] = str(e)
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
    return info


def main() -> int:
    try:
        import pymysql
    except ImportError:
        print("# pymysql not installed; cannot diagnose.", file=sys.stderr)
        print("# Run inside `docker exec zhanlu-backend python ...` "
              "or activate a venv that has pymysql.", file=sys.stderr)
        return 1

    kb = get_kb_config_via_env() or get_kb_config_via_docker()
    if not kb:
        print("# Could not resolve warehouse KB config.", file=sys.stderr)
        return 2

    print(f"# KB: {kb['kb_id']}  host={kb['host']}:{kb['port']}  db={kb['database_name']}")
    print()

    try:
        conn = pymysql.connect(
            host=kb["host"],
            port=kb["port"],
            user=kb["username"],
            password=kb["password"],
            database=kb["database_name"],
            connect_timeout=10,
        )
    except Exception as e:
        print(f"# Connection failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 3

    print(f"{'TABLE':<40} {'ROWS':>8}  {'DATE_COL':<25}  {'DATE_RANGE'}")
    print("-" * 100)
    for table in CANDIDATE_TABLES:
        info = diagnose_table(conn, table)
        if "error" in info:
            print(f"{table:<40} {'ERR':>8}  {info['error'][:60]}")
            continue
        rows = info.get("rows", 0)
        date_col = info.get("date_col", "-") or "-"
        dr = info.get("date_range", ["-", "-"])
        dr_str = f"{dr[0]} -> {dr[1]}" if dr != ["-", "-"] else "-"
        print(f"{table:<40} {rows:>8}  {date_col:<25}  {dr_str}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
