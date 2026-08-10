#!/usr/bin/env python3
"""One-time migration: update forecast targets with ERP primary datasources.

Instead of deleting (which fails due to FK constraint from forecast_runs),
this script UPDATES the datasource JSON in-place for existing targets,
then runs dynamic SKU discovery for new product families.

Usage:
    docker cp backend/scripts/migrate_erp_datasources.py zhanlu-backend:/app/scripts/
    docker exec zhanlu-backend python /app/scripts/migrate_erp_datasources.py
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, "/app")
os.environ.setdefault("SQLALCHEMY_WARN_20", "0")


def main():
    from app.database import SessionLocal
    from app.models.forecasting import ForecastTarget
    from app.services.forecasting.seed_ecisco_targets import (
        ECISCO_FORECAST_TARGETS,
        discover_and_seed_sku_targets,
    )

    db = SessionLocal()
    try:
        # 1. Update existing static targets with new datasource configs
        print("=" * 70)
        print("STEP 1: Update existing targets with ERP datasources")
        print("=" * 70)

        updated = 0
        for spec in ECISCO_FORECAST_TARGETS:
            pk = spec["product_key"]
            existing = db.query(ForecastTarget).filter(
                ForecastTarget.product_key == pk,
                ForecastTarget.is_deleted == False,  # noqa: E712
            ).first()

            if existing is None:
                print(f"  SKIP (not found): {pk}")
                continue

            old_table = existing.datasource.get("table", "?") if existing.datasource else "?"
            new_table = spec["datasource"].get("table", "?")

            if old_table == new_table:
                print(f"  SKIP (already up-to-date): {pk} -> {new_table}")
                continue

            # Update datasource and name
            existing.datasource = spec["datasource"]
            existing.name = spec["name"]
            db.flush()
            updated += 1
            print(f"  UPDATED: {pk}")
            print(f"    old: {old_table}")
            print(f"    new: {new_table}")

        db.commit()
        print(f"\n  Updated {updated} targets.\n")

        # 2. Insert any missing static targets
        print("=" * 70)
        print("STEP 2: Insert missing static targets")
        print("=" * 70)
        from app.services.forecasting.seed_ecisco_targets import seed_ecisco_forecast_targets
        n_static = seed_ecisco_forecast_targets(db)
        print(f"  Inserted {n_static} new static targets.\n")

        # 3. Run dynamic SKU discovery (creates targets for new ERP tables)
        print("=" * 70)
        print("STEP 3: Dynamic SKU discovery for ERP tables")
        print("=" * 70)
        n_dynamic = discover_and_seed_sku_targets(db)
        print(f"  Inserted {n_dynamic} new dynamic SKU targets.\n")

        # 4. Summary
        print("=" * 70)
        print("SUMMARY: All ecisco.* forecast targets")
        print("=" * 70)
        all_targets = db.query(ForecastTarget).filter(
            ForecastTarget.product_key.like("ecisco.%"),
            ForecastTarget.is_deleted == False,  # noqa: E712
        ).order_by(ForecastTarget.report_order, ForecastTarget.product_key).all()

        for t in all_targets:
            ds = t.datasource or {}
            table = ds.get("table", "?")
            n_extra = len(ds.get("extra_sources", []))
            print(f"  {t.product_key:<55} table={table:<40} extras={n_extra}")

        print(f"\nTotal: {len(all_targets)} targets")
        print("\nDone. Restart backend: docker restart zhanlu-backend")

    except Exception as e:
        db.rollback()
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
