"""add resource_type to projects+agent_apps and create resource_shares table

Revision ID: 038
Revises: 037
Create Date: 2026-08-03

Adds a ``resource_type`` column ('company'/'personal') to both
``projects`` and ``agent_apps``.  The column is derived from the
creator's role at creation time (admin→'company', user→'personal')
and is stamped server-side — clients can never change it via PUT.

Backfill rule (runs once):
  is_system=true OR created_by_id IS NULL → 'company'
  everything else → 'personal'

Also creates the ``resource_shares`` polymorphic ACL table so owners
can share their resources with other users for View+Use access.
"""

from alembic import op
import sqlalchemy as sa


revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Add resource_type to projects ──────────────────────────
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(
            sa.Column(
                "resource_type",
                sa.String(20),
                nullable=False,
                server_default="personal",
            ),
        )
        batch_op.create_index(
            "ix_projects_resource_type",
            ["resource_type"],
        )

    # Backfill projects: is_system=true OR created_by_id IS NULL → 'company'
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE projects SET resource_type = 'company' "
            "WHERE (is_system = TRUE OR created_by_id IS NULL) "
            "AND is_deleted = FALSE"
        )
    )

    # ── 2. Add resource_type to agent_apps ────────────────────────
    with op.batch_alter_table("agent_apps") as batch_op:
        batch_op.add_column(
            sa.Column(
                "resource_type",
                sa.String(20),
                nullable=False,
                server_default="personal",
            ),
        )
        batch_op.create_index(
            "ix_agent_apps_resource_type",
            ["resource_type"],
        )

    # Backfill agent_apps: is_system=true OR created_by_id IS NULL → 'company'
    bind.execute(
        sa.text(
            "UPDATE agent_apps SET resource_type = 'company' "
            "WHERE (is_system = TRUE OR created_by_id IS NULL) "
            "AND is_deleted = FALSE"
        )
    )

    # ── 3. Create resource_shares table ───────────────────────────
    op.create_table(
        "resource_shares",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=False),
        sa.Column("updated_date", sa.DateTime(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        # created_by_id = shared_by_user_id (the owner who shared)
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("org_id", sa.String(36), nullable=False,
                  server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False,
                  server_default="default-app"),
        # Polymorphic target
        sa.Column("resource_type", sa.String(20), nullable=False),
        sa.Column("resource_id", sa.String(36), nullable=False),
        # Who was granted access
        sa.Column("shared_with_user_id", sa.String(36), nullable=False),
        # Access level: 'use' = view+run (current); 'view' reserved
        sa.Column("access_level", sa.String(20), nullable=False,
                  server_default="use"),
    )

    with op.batch_alter_table("resource_shares") as batch_op:
        batch_op.create_index(
            "ix_resource_shares_target",
            ["resource_type", "resource_id"],
        )
        batch_op.create_index(
            "ix_resource_shares_shared_with",
            ["shared_with_user_id"],
        )
        batch_op.create_index(
            "ix_resource_shares_org_app",
            ["org_id", "app_id"],
        )


def downgrade() -> None:
    # ── Drop resource_shares table ──
    op.drop_table("resource_shares")

    # ── Drop resource_type from projects ──
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_index("ix_projects_resource_type")
        batch_op.drop_column("resource_type")

    # ── Drop resource_type from agent_apps ──
    with op.batch_alter_table("agent_apps") as batch_op:
        batch_op.drop_index("ix_agent_apps_resource_type")
        batch_op.drop_column("resource_type")
