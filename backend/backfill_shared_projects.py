"""One-time backfill: promote already-shared projects from 'personal' to 'company'.

Run once after deploying the resource_shares.py auto-flip hook.  Finds every
Project that has at least one active ResourceShare and is still marked
``resource_type='personal'``, then flips it to ``'company'`` so it appears
under Company Projects for all users.

Safe to re-run — already-promoted projects are skipped.
"""

from app.database import SessionLocal
from app.models.project import Project
from app.models.resource_share import ResourceShare


def backfill():
    db = SessionLocal()
    try:
        # Find all project IDs that have at least one active share
        shared_ids = (
            db.query(ResourceShare.resource_id)
            .filter(
                ResourceShare.resource_type == "project",
                ResourceShare.is_deleted == False,
            )
            .distinct()
            .all()
        )
        shared_ids = [row[0] for row in shared_ids]

        if not shared_ids:
            print("No shared projects found — nothing to do.")
            return

        # Among those, flip any still marked 'personal' to 'company'
        personal_shared = (
            db.query(Project)
            .filter(
                Project.id.in_(shared_ids),
                Project.resource_type == "personal",
                Project.is_deleted == False,
            )
            .all()
        )

        if not personal_shared:
            print("All shared projects are already 'company' — nothing to do.")
            return

        for proj in personal_shared:
            proj.resource_type = "company"
            print(f"  ✓ {proj.id}  {proj.name or proj.title or '(untitled)'}")

        db.commit()
        print(f"\nDone — {len(personal_shared)} projects promoted to Company Projects.")
    finally:
        db.close()


if __name__ == "__main__":
    backfill()
