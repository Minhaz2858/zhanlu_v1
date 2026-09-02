"""Seed well-known LLM catalog rows (kimi-k2, MiniMax M2, ...) manually.

Idempotent: rows are inserted only when no row with the same ``model_id``
exists for the tenant, so admin edits are never overwritten. The same
seeding also runs automatically on backend startup; this script is for
refreshing an already-running deployment without a restart.

Run:  python scripts/seed_llm_catalog.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

from app.database import Base, SessionLocal, engine
from app.services.llm_catalog import seed_llm_catalog, should_seed_catalog


def main() -> int:
    if not should_seed_catalog():
        print("LLM_CATALOG_SEED_MODE=skip — nothing to do.")
        return 0
    # Idempotent: creates missing tables (e.g. a fresh DB), no-ops when
    # the schema already exists (normal prestart: alembic upgrade head).
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        created = seed_llm_catalog(db)
        print(f"Seeded {len(created)} well-known LLM catalog row(s).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
