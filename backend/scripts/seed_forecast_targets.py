#!/usr/bin/env python3
"""CLI: seed ForecastTarget rows for the Ecisco BI project.

Registers 9 ForecastTarget rows for products that have data in the MySQL
warehouse's LZ views. Idempotent — safe to run multiple times.

Usage:
    docker exec zhanlu-backend python /app/scripts/seed_forecast_targets.py
    docker exec zhanlu-backend python /app/scripts/seed_forecast_targets.py --org-id org-1 --app-id ecisco-bi
"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import SessionLocal  # noqa: E402
from app.services.forecasting.seed_targets import seed_forecast_targets  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-id", default="default-org", help="org_id (tenant)")
    parser.add_argument("--app-id", default="ecisco-bi", help="app_id (project)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        n = seed_forecast_targets(db, org_id=args.org_id, app_id=args.app_id)
        print(f"Created {n} ForecastTarget rows for org={args.org_id}, app={args.app_id}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
