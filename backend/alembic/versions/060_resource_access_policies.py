"""Create resource_access_policies table.

Revision ID: 060_resource_access_policies
Revises: 059_add_read_to_reports_and_automation_files
Create Date: 2026-08-14

Per-user, per-KB, per-table data access rules layered on top of the existing
``resource_shares`` system.  Default semantics are allow-all: when no policy
rows exist for a (user, resource) tuple, the shared user sees every KB and
table granted by the share.  Policies only apply when an owner explicitly
creates them.

Columns mirror ``ResourceAccessPolicy`` model:
- ``resource_share_id``  FK -> resource_shares.id (policy is scoped to a share)
- ``resource_type`` / ``resource_id``  denormalized resource target
- ``user_id``            FK -> users.id (the shared user being constrained)
- ``kb_id``              NULL = all KBs; else a specific KnowledgeBase
- ``table_name``         NULL = all tables in KB; else a specific table
- ``mode``               'allow' | 'deny' | 'allow_columns'
- ``column_allowlist``   JSON list (mode='allow_columns')
- ``row_filter``         JSON dict (optional row-level filter)

Indexes support the two hot lookup shapes: by share (cascade cleanup on revoke)
and by (user, kb, table) for runtime resolution.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "060_resource_access_policies"
down_revision = "059_add_read_to_reports_and_automation_files"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "resource_access_policies",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        sa.Column("resource_share_id", sa.String(36), nullable=False),
        sa.Column("resource_type", sa.String(20), nullable=False),
        sa.Column("resource_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("kb_id", sa.String(36), nullable=True),
        sa.Column("table_name", sa.String(256), nullable=True),
        sa.Column("mode", sa.String(20), nullable=False, server_default="allow"),
        sa.Column("column_allowlist", sa.JSON(), nullable=True),
        sa.Column("row_filter", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["resource_share_id"], ["resource_shares.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )

    # Partial unique index so the batch-upsert endpoint can soft-delete old rows
    # and re-insert the same (kb_id, table_name) without a unique conflict.
    op.create_index(
        "uq_resource_access_policy_share_kb_table",
        "resource_access_policies",
        ["resource_share_id", "kb_id", "table_name"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )

    op.create_index(
        "ix_resource_access_policies_share",
        "resource_access_policies",
        ["resource_share_id"],
    )
    op.create_index(
        "ix_resource_access_policies_resource",
        "resource_access_policies",
        ["resource_type", "resource_id"],
    )
    op.create_index(
        "ix_resource_access_policies_user_kb_table",
        "resource_access_policies",
        ["user_id", "kb_id", "table_name"],
    )
    op.create_index(
        "ix_resource_access_policies_org",
        "resource_access_policies",
        ["org_id"],
    )
    op.create_index(
        "ix_resource_access_policies_app",
        "resource_access_policies",
        ["app_id"],
    )


def downgrade():
    op.drop_index("uq_resource_access_policy_share_kb_table", table_name="resource_access_policies")
    op.drop_index("ix_resource_access_policies_app", table_name="resource_access_policies")
    op.drop_index("ix_resource_access_policies_org", table_name="resource_access_policies")
    op.drop_index("ix_resource_access_policies_user_kb_table", table_name="resource_access_policies")
    op.drop_index("ix_resource_access_policies_resource", table_name="resource_access_policies")
    op.drop_index("ix_resource_access_policies_share", table_name="resource_access_policies")
    op.drop_table("resource_access_policies")
