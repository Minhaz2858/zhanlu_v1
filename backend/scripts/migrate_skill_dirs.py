#!/usr/bin/env python3
"""Phase 11 (S1) — migrate legacy user skills into per-org directories.

Before this migration every tenant's installed skills sat in the SHARED
``~/.zhanlu/skills/<category>/<name>/`` root (visible to all tenants).
This script, run ONCE per deployment AFTER alembic 133, moves each tenant-
owned skill into ``~/.zhanlu/skills/orgs/<org_id>/<category>/<name>/``
based on the ``tools`` table (source in custom/external/marketplace_user,
created_by_id → users.org_id).

Rules:
* Idempotent — a skill already in the org dir is skipped; running twice
  is a no-op.
* Unresolvable ownership (no Tool row, owner deleted, org unknown) — the
  file STAYS in the legacy root and is reported. Legacy-root skills are
  the system/legacy tier (visible to all orgs) — deliberate fail-safe:
  we never guess ownership and never silently reassign it.
* Auditable — every move is logged and a summary printed.

Usage: python scripts/migrate_skill_dirs.py [--dry-run]
"""
import argparse
import logging
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("migrate_skill_dirs")

TENANT_SOURCES = ("custom", "external", "marketplace_user")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from app.database import SessionLocal
    from app.models.tool import Tool
    from app.models.user import User
    from app.services.skill_sync import USER_SKILLS_DIR

    db = SessionLocal()
    moved, skipped_in_place, unresolvable = 0, 0, []
    try:
        rows = db.query(Tool).filter(
            Tool.is_deleted == False,  # noqa: E712
            Tool.source.in_(TENANT_SOURCES),
        ).all()
        user_org = {
            u.id: getattr(u, "org_id", None)
            for u in db.query(User).all()
        }
        for tool in rows:
            org = user_org.get(tool.created_by_id or "")
            if not org:
                unresolvable.append(tool.name)
                continue
            src = Path(USER_SKILLS_DIR) / (tool.category or "custom") / tool.name
            dst = (Path(USER_SKILLS_DIR) / "orgs" / org
                   / (tool.category or "custom") / tool.name)
            if not src.exists():
                skipped_in_place += 1
                continue
            if dst.exists():
                log.info("SKIP (already in org dir): %s → %s", tool.name, dst)
                skipped_in_place += 1
                continue
            if args.dry_run:
                log.info("WOULD MOVE %s → %s", src, dst)
                moved += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            log.info("MOVED %s → %s", src, dst)
            moved += 1
    finally:
        db.close()

    log.info("--- summary: moved=%d already_moved=%d unresolvable=%d%s",
             moved, skipped_in_place, len(unresolvable),
             f" ({', '.join(sorted(set(unresolvable)))})"
             if unresolvable else "")
    if unresolvable and not args.dry_run:
        log.warning(
            "Skills with unresolvable ownership were LEFT in the legacy "
            "root (system/legacy tier — visible to all orgs). Review and "
            "either assign them a Tool row with created_by_id, or delete "
            "them deliberately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
