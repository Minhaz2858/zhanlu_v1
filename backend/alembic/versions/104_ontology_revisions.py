"""P2 versioned ontology registry — append-only ontology_revisions table.

RLS inline (same tenant_isolation policy form as 094). History is never
updated or deleted; rollback appends a new revision.

Revision ID: 104_ontology_revisions
Revises: 103_ontology_evidence
Create Date: 2026-10-30
"""

from alembic import op
import sqlalchemy as sa

revision = "104_ontology_revisions"
down_revision = "103_ontology_evidence"
branch_labels = None
depends_on = None


def _rls(table: str) -> None:
    op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} USING ("
        "  coalesce(current_setting('app.current_org', true), '') = ''"
        "  OR org_id = current_setting('app.current_org', true)"
        ")"
    )


def upgrade() -> None:
    op.create_table(
        "ontology_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False, index=True),
        sa.Column("app_id", sa.String(36), nullable=True),
        sa.Column("entity_type", sa.String(20), nullable=False),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("change_kind", sa.String(20), nullable=False),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=True),
        sa.Column("changed_by", sa.String(255), nullable=True),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.UniqueConstraint("entity_type", "entity_id", "revision_no",
                            name="uq_ontology_revision_no"),
    )
    op.create_index("ix_ontology_revisions_entity",
                    "ontology_revisions",
                    ["org_id", "entity_type", "entity_id"])
    op.create_index("ix_ontology_revisions_change_kind",
                    "ontology_revisions", ["change_kind"])
    _rls("ontology_revisions")


def downgrade() -> None:
    op.drop_index("ix_ontology_revisions_change_kind",
                  table="ontology_revisions")
    op.drop_index("ix_ontology_revisions_entity", table="ontology_revisions")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON ontology_revisions")
    op.drop_table("ontology_revisions")
