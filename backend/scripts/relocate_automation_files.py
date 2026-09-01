"""One-off migration: move automation-generated files out of the public
uploads tree and rewrite AutomationFile rows to the authenticated download
route. Idempotent — safe to run repeatedly.

Usage: python3 scripts/relocate_automation_files.py  (from backend/)
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.database import SessionLocal
from app.models.automation_file import AutomationFile


def relocate(db) -> dict:
    old_root = (settings.upload_path / "automation").resolve()
    new_root = (settings.generated_path / "automation").resolve()
    stats = {"moved": 0, "updated": 0, "skipped": 0, "missing": 0}
    rows = db.query(AutomationFile).filter(AutomationFile.is_deleted == False).all()  # noqa: E712
    for row in rows:
        p = Path(row.file_path or "")
        resolved = p.resolve() if str(p) else None
        if resolved is not None and str(resolved).startswith(str(new_root)):
            stats["skipped"] += 1
            continue
        try:
            rel = resolved.relative_to(old_root) if resolved else None
        except ValueError:
            rel = None
        if rel is None:
            stats["missing"] += 1
            row.file_url = f"/api/automations/files/{row.id}/download"
            stats["updated"] += 1
            continue
        target = new_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and not target.exists():
            shutil.move(str(p), str(target))
            stats["moved"] += 1
        row.file_path = str(target)
        row.file_url = f"/api/automations/files/{row.id}/download"
        stats["updated"] += 1
    db.commit()
    return stats


def main() -> None:
    db = SessionLocal()
    try:
        print(relocate(db))
    finally:
        db.close()


if __name__ == "__main__":
    main()
