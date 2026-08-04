"""One-time backfill: promote already-shared agents from 'personal' to 'company'.

Run once after deploying the resource_shares.py auto-flip hook.  Finds every
AgentApp that has at least one active ResourceShare and is still marked
``resource_type='personal'``, then flips it to ``'company'`` so it appears
under Company Agents for all users.

Safe to re-run — already-promoted agents are skipped.
"""

from sqlalchemy import text

from app.database import SessionLocal
from app.models.agent_app import AgentApp
from app.models.resource_share import ResourceShare


def backfill():
    db = SessionLocal()
    try:
        # Find all agent IDs that have at least one active share
        shared_ids = (
            db.query(ResourceShare.resource_id)
            .filter(
                ResourceShare.resource_type == "agent",
                ResourceShare.is_deleted == False,
            )
            .distinct()
            .all()
        )
        shared_ids = [row[0] for row in shared_ids]

        if not shared_ids:
            print("No shared agents found — nothing to do.")
            return

        # Among those, flip any still marked 'personal' to 'company'
        personal_shared = (
            db.query(AgentApp)
            .filter(
                AgentApp.id.in_(shared_ids),
                AgentApp.resource_type == "personal",
                AgentApp.is_deleted == False,
            )
            .all()
        )

        if not personal_shared:
            print("All shared agents are already 'company' — nothing to do.")
            return

        for agent in personal_shared:
            agent.resource_type = "company"
            print(f"  ✓ {agent.id}  {agent.name or agent.title or '(untitled)'}")

        db.commit()
        print(f"\nDone — {len(personal_shared)} agents promoted to Company Agents.")
    finally:
        db.close()


if __name__ == "__main__":
    backfill()
