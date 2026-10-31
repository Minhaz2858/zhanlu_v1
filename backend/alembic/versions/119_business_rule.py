"""119_business_rule — unified evidence-backed business rule model.

Revision ID: 119_business_rule
Revises: 118_kb_multi_project
Create Date: 2026-11-01

WHY
---
Phase 1A of the Enterprise Knowledge Model: ONE authoritative home for
business rules so the same rule can serve NL2SQL prompt guidelines,
business-process reasoning and governed action pre-checks. Rules are born
``proposed`` and NEVER auto-enabled — only human review moves them to
``verified`` / ``rejected`` / ``enabled``. Fully additive; no existing
table or row is touched.

CHANGE (additive only)
----------------------
1. New table ``business_rules`` (org/app-scoped, soft-delete via
   ``is_deleted`` — TimestampedBase conventions).
2. CheckConstraints enforce the scope/status/applies_to vocabularies and
   require scoped rules to name a target.
3. Indexes for the two hot lookups: (org, status) review queues and
   (org, scope, scope_ref_id) runtime consumption.

Safe on production data: empty table at creation, no backfill.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "119_business_rule"
down_revision: Union[str, Sequence[str], None] = "118_kb_multi_project"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "business_rules",
        sa.Column("id", sa.String(36), primary_key=True),
        # org | domain | object | process
        sa.Column("scope", sa.String(20), nullable=False,
                  server_default="org"),
        sa.Column("scope_ref_id", sa.String(36), nullable=True),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("source_evidence_ids", sa.JSON(), nullable=True),
        # proposed | verified | rejected | enabled
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="proposed"),
        # sql | action | process
        sa.Column("applies_to", sa.String(20), nullable=False,
                  server_default="sql"),
        sa.Column("review_history", sa.JSON(), nullable=True),
        # Tenant columns — required by the forced RLS policy below.
        sa.Column("org_id", sa.String(36), nullable=False,
                  server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False,
                  server_default="default-app"),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.CheckConstraint(
            "scope IN ('org', 'domain', 'object', 'process')",
            name="ck_business_rule_scope",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'verified', 'rejected', 'enabled')",
            name="ck_business_rule_status",
        ),
        sa.CheckConstraint(
            "applies_to IN ('sql', 'action', 'process')",
            name="ck_business_rule_applies_to",
        ),
        sa.CheckConstraint(
            "scope = 'org' OR scope_ref_id IS NOT NULL",
            name="ck_business_rule_scoped_needs_ref",
        ),
    )
    op.create_index(
        "ix_business_rules_org_status", "business_rules",
        ["org_id", "status"],
    )
    op.create_index(
        "ix_business_rules_org_scope", "business_rules",
        ["org_id", "scope", "scope_ref_id"],
    )
    op.create_index(
        "ix_business_rules_scope_ref_id", "business_rules", ["scope_ref_id"],
    )
    op.create_index(
        "ix_business_rules_status", "business_rules", ["status"],
    )

    # Same tenant policy as every other table (see 102_turn_identity).
    op.execute("ALTER TABLE business_rules ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE business_rules FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON business_rules USING ("
        "  coalesce(current_setting('app.current_org', true), '') = ''"
        "  OR org_id = current_setting('app.current_org', true)"
        ")"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON business_rules")
    op.drop_index("ix_business_rules_status", table_name="business_rules")
    op.drop_index("ix_business_rules_scope_ref_id",
                  table_name="business_rules")
    op.drop_index("ix_business_rules_org_scope", table_name="business_rules")
    op.drop_index("ix_business_rules_org_status", table_name="business_rules")
    op.drop_table("business_rules")
