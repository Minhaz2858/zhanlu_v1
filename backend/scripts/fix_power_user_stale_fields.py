"""One-shot fixup: align the existing power_user DB row with the
system_agents.py source of truth.

Before this fix:
  - project_id = NULL        (should be Global project)
  - model       = "enterprise" (should be "deepseek-chat")
  - created_by_id = NULL     (should be admin user, matching the other 4 system agents)

This script resolves the Global project and admin user at runtime so
it never hardcodes IDs. Safe to re-run — it's idempotent.
"""

import logging
import sys

sys.path.insert(0, ".")  # allow running from backend/

from app.database import SessionLocal
from app.models.agent_app import AgentApp
from app.models.project import Project
from app.models.user import User

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def fix_power_user():
    db = SessionLocal()
    try:
        # Resolve Global project
        gp = db.query(Project).filter(
            Project.name == "Global",
            Project.is_deleted == False,
        ).first()
        if not gp:
            logger.error("Global project not found — cannot fix.")
            return False

        # Resolve admin user (same created_by_id used by agent_builder etc.)
        admin = db.query(User).filter(
            User.email == "admin@zhanlu.dev",
            User.is_deleted == False,
        ).first()
        if not admin:
            logger.error("Admin user not found — cannot fix.")
            return False

        # Find power_user
        pw = db.query(AgentApp).filter(
            AgentApp.name == "power_user",
            AgentApp.is_deleted == False,
        ).first()
        if not pw:
            logger.error("power_user not found — nothing to fix.")
            return False

        changes = []
        if pw.project_id != gp.id:
            changes.append(f"project_id: {pw.project_id} → {gp.id}")
            pw.project_id = gp.id
        if pw.model != "deepseek-chat":
            changes.append(f'model: "{pw.model}" → "deepseek-chat"')
            pw.model = "deepseek-chat"
        if pw.created_by_id != admin.id:
            changes.append(f"created_by_id: {pw.created_by_id} → {admin.id}")
            pw.created_by_id = admin.id

        if not changes:
            logger.info("power_user is already up to date — nothing to fix.")
            return True

        db.add(pw)
        db.commit()
        logger.info("power_user fixed: %s", "; ".join(changes))

        # Print confirmation
        print("\n=== power_user after fix ===")
        db.refresh(pw)
        print(f"  project_id    = {pw.project_id}")
        print(f"  model         = {pw.model}")
        print(f"  created_by_id = {pw.created_by_id}")
        print(f"  status        = {pw.status}")
        print(f"  tools         = {len((pw.tool_config or {}).get('enabled_tools', []))}")
        return True
    except Exception as exc:
        db.rollback()
        logger.exception("fix_power_user failed: %s", exc)
        return False
    finally:
        db.close()


if __name__ == "__main__":
    ok = fix_power_user()
    sys.exit(0 if ok else 1)
