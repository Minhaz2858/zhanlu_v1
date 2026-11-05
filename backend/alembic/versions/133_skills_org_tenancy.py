"""skills org tenancy — populate tools.org_id from owning users

Revision ID: 133_skills_org_tenancy
Revises: 132_automation_retry_scheduling
Create Date: 2026-11-03

Phase 11 (S1): the ``tools`` table has carried an ``org_id`` column
(TimestampedBase default 'default-org') that was never populated for
tenant-installed skills — every skill effectively lived in the legacy
default tier, readable by all tenants. This migration backfills
``tools.org_id`` from the owning user's organization for tenant sources
(``custom``, ``marketplace_user``).

Repeatable & auditable:
* Idempotent — only rows still at the legacy default are touched.
* Preserves history — rows whose owner cannot be resolved stay at
  'default-org' (system/legacy tier) and are reported via WARNING logs.
* No column added/dropped; rollback is a no-op (values can be re-derived).
"""
import logging

from alembic import op
from sqlalchemy import text

revision = "133_skills_org_tenancy"
down_revision = "132_automation_retry_scheduling"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.migration")


def upgrade() -> None:
    # Postgres UPDATE...FROM (the deployment DB). SQLite test databases do
    # not run Alembic; the equivalent logic is exercised as a unit test
    # (test_skills_org_tenancy.py::test_migration_133_backfill_logic).
    conn = op.get_bind()
    result = conn.execute(
        text(
            """
            UPDATE tools AS t
               SET org_id = u.org_id
              FROM users AS u
             WHERE t.created_by_id = u.id
               AND u.org_id IS NOT NULL
               AND t.org_id = 'default-org'
               AND t.source IN ('custom', 'marketplace_user')
            """
        )
    )
    logger.warning(
        "133_skills_org_tenancy: backfilled tools.org_id for %s tenant "
        "skill row(s); unresolvable owners stay in the legacy tier",
        getattr(result, "rowcount", "?"),
    )


def downgrade() -> None:
    # Backfilled values are re-derivable; no schema change was made.
    pass
