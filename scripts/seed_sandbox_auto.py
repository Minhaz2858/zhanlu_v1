#!/usr/bin/env python3
"""Create the automotive-sales sandbox enterprise DB for the P0 Phase 2 pilot.

Deterministic, idempotent. Run inside the zhanlu-postgres container's
companion backend (it has psycopg2) or any python with a superuser DSN:

    docker exec -w /app -e PYTHONPATH=/app zhanlu-backend python \
        /app/scripts/seed_sandbox_auto.py

Schema (automobile manufacturer sales, deliberately generic naming so the
semantic layer has to do real mapping work):
  vehicle_model   model_code PK, model_name, segment, launch_year
  dealer          dealer_code PK, dealer_name, region
  sales_order     order_id PK, order_date, model_code FK, dealer_code FK,
                  units, unit_price, revenue          (grain: order)
  monthly_sales   month, model_code FK, units_sold, avg_price, revenue
                                                      (grain: model-month)
  production_plan month, model_code FK, planned_units (grain: model-month)

Data: 36 months (2023-10 .. 2026-09), 4 models, 4 dealers, seasonal+trend,
deterministic hash-based noise. Constructed so "Falcon SUV" is the clear
2025 revenue leader (verifiable acceptance figure).
"""
import hashlib
import os

import psycopg2

ADMIN_DSN = os.environ.get("SANDBOX_ADMIN_DSN", "")
DB = "zhanlu_sandbox_auto"

MODELS = [
    ("Falcon SUV", "FALCON", "SUV", 2021, 620, 0.004, 0.30),
    ("Aurora EV3", "AURORA", "EV-Compact", 2023, 260, 0.020, 0.15),
    ("Sentinel Sedan", "SENTINEL", "Sedan", 2018, 410, -0.008, 0.10),
    ("Nomad Van", "NOMAD", "Van", 2020, 180, 0.002, 0.20),
]
DEALERS = [
    ("DLR-EAST", "Eastgate Motors", "East"),
    ("DLR-WEST", "Pacific Avenue Auto", "West"),
    ("DLR-NORTH", "Northern Star Vehicles", "North"),
    ("DLR-SOUTH", "Sunbelt Auto Group", "South"),
]
MONTHS = [
    (y, m)
    for y in range(2023, 2027)
    for m in range(1, 13)
    if (y, m) >= (2023, 10) and (y, m) <= (2026, 9)
]
PRICE = {"FALCON": 38500.0, "AURORA": 29900.0, "SENTINEL": 24800.0, "NOMAD": 32400.0}


def _noise(*key) -> float:
    """Deterministic ±1 multiplier noise in [0.92, 1.08]."""
    h = int(hashlib.md5("|".join(str(k) for k in key).encode()).hexdigest()[:8], 16)
    return 0.92 + (h % 160) / 1000.0


def _units(model, y, m):
    code, base, trend, season_amp = model[1], model[4], model[5], model[6]
    idx = MONTHS.index((y, m))
    seasonal = 1.0 + season_amp * ((-1) ** (m in (6, 7, 8)) and (m in (6, 7, 8)) or 0)
    # simple summer peak for all models, stronger for SUV
    seasonal = 1.0 + season_amp * (1 if m in (5, 6, 7, 8) else (-0.35 if m in (1, 2) else 0))
    growth = (1.0 + trend) ** idx
    return max(10, int(base * seasonal * growth * _noise(code, y, m)))


def _creds():
    """Credentials for TCP auth. Prefers the app's own DATABASE_URL (the
    volume's real init password may differ from the current POSTGRES_PASSWORD
    env). Returns (host, user, password)."""
    url = os.environ.get("DATABASE_URL", "")
    if url:
        from sqlalchemy.engine import make_url

        u = make_url(url)
        return (
            u.host or "postgres",
            u.username or "zhanlu_app",
            u.password or "",
        )
    return (
        os.environ.get("SANDBOX_PG_HOST", "postgres"),
        "zhanlu",
        os.environ.get("POSTGRES_PASSWORD", ""),
    )


def main():
    host, user, password = _creds()
    try:
        admin = psycopg2.connect(
            host=host, port=5432, dbname="postgres", user=user, password=password,
        )
    except psycopg2.OperationalError:
        # No superuser via TCP — assume the DB already exists (created via
        # local-trust psql inside the postgres container).
        admin = None
    if admin is not None:
        admin.autocommit = True
        cur = admin.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB,))
        if not cur.fetchone():
            cur.execute(f'CREATE DATABASE "{DB}"')
            print(f"created database {DB}")
        else:
            print(f"database {DB} exists")
        admin.close()

    conn = psycopg2.connect(
        host=host, port=5432, dbname=DB, user=user, password=password,
    )
    conn.autocommit = False
    c = conn.cursor()
    c.execute(
        """
        DROP TABLE IF EXISTS sales_order, monthly_sales, production_plan,
                              vehicle_model, dealer CASCADE;
        CREATE TABLE vehicle_model (
            model_code VARCHAR(20) PRIMARY KEY,
            model_name VARCHAR(80) NOT NULL,
            segment VARCHAR(30) NOT NULL,
            launch_year INTEGER NOT NULL,
            msbp NUMERIC(10,2) NOT NULL
        );
        CREATE TABLE dealer (
            dealer_code VARCHAR(20) PRIMARY KEY,
            dealer_name VARCHAR(80) NOT NULL,
            region VARCHAR(30) NOT NULL
        );
        CREATE TABLE sales_order (
            order_id VARCHAR(24) PRIMARY KEY,
            order_date DATE NOT NULL,
            model_code VARCHAR(20) NOT NULL REFERENCES vehicle_model(model_code),
            dealer_code VARCHAR(20) NOT NULL REFERENCES dealer(dealer_code),
            units INTEGER NOT NULL,
            unit_price NUMERIC(10,2) NOT NULL,
            revenue NUMERIC(12,2) NOT NULL
        );
        CREATE TABLE monthly_sales (
            month DATE NOT NULL,
            model_code VARCHAR(20) NOT NULL REFERENCES vehicle_model(model_code),
            units_sold INTEGER NOT NULL,
            avg_price NUMERIC(10,2) NOT NULL,
            revenue NUMERIC(12,2) NOT NULL,
            PRIMARY KEY (month, model_code)
        );
        CREATE TABLE production_plan (
            month DATE NOT NULL,
            model_code VARCHAR(20) NOT NULL REFERENCES vehicle_model(model_code),
            planned_units INTEGER NOT NULL,
            PRIMARY KEY (month, model_code)
        );
        COMMENT ON TABLE sales_order IS 'Confirmed vehicle sales orders, one row per order';
        COMMENT ON TABLE monthly_sales IS 'Monthly aggregated vehicle sales per model';
        COMMENT ON TABLE production_plan IS 'Monthly production plan per model';
        """
    )

    for name, code, seg, launch, _base, _t, _s in MODELS:
        c.execute(
            "INSERT INTO vehicle_model VALUES (%s,%s,%s,%s,%s)",
            (code, name, seg, launch, PRICE[code]),
        )
    for code, name, region in DEALERS:
        c.execute("INSERT INTO dealer VALUES (%s,%s,%s)", (code, name, region))

    order_n = 0
    for (y, m) in MONTHS:
        for model in MODELS:
            code = model[1]
            units = _units(model, y, m)
            price = PRICE[code] * _noise("price", code, y, m)
            rev = round(units * price, 2)
            c.execute(
                "INSERT INTO monthly_sales VALUES (%s,%s,%s,%s,%s)",
                (f"{y}-{m:02d}-01", code, units, round(price, 2), rev),
            )
            c.execute(
                "INSERT INTO production_plan VALUES (%s,%s,%s)",
                (f"{y}-{m:02d}-01", code, int(units * 1.05)),
            )
            # split the monthly units into 3 deterministic dealer orders
            per = [int(units * w) for w in (0.5, 0.3, 0.2)]
            for d_i, d_units in enumerate(per):
                if d_units <= 0:
                    continue
                order_n += 1
                day = 3 + d_i * 9
                c.execute(
                    "INSERT INTO sales_order VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        f"SO-{y}{m:02d}-{code}-{d_i}",
                        f"{y}-{m:02d}-{day:02d}",
                        code, DEALERS[d_i][0], d_units,
                        round(price, 2), round(d_units * price, 2),
                    ),
                )
    conn.commit()

    c.execute(
        "SELECT model_code, SUM(revenue) FROM sales_order "
        "WHERE order_date BETWEEN '2025-01-01' AND '2025-12-31' "
        "GROUP BY model_code ORDER BY 2 DESC"
    )
    print("2025 revenue by model (acceptance reference):")
    for code, rev in c.fetchall():
        print(f"  {code}: {rev:,.2f}")
    conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
