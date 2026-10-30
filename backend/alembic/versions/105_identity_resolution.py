"""P3/P3a — identity resolution: file row staging + identity crosswalks.

Revision ID: 105_identity_resolution
Revises: 104_ontology_revisions
Create Date: 2026-10-30
"""

from alembic import op
import sqlalchemy as sa

revision = "105_identity_resolution"
down_revision = "104_ontology_revisions"
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


def _common_cols() -> list[sa.Column]:
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False, index=True),
        sa.Column("app_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "file_table_schemas",
        *_common_cols(),
        sa.Column("kb_id", sa.String(36), nullable=False, index=True),
        sa.Column("sheet_name", sa.String(255), nullable=False),
        sa.Column("columns_json", sa.JSON(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truncated", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("parsed_at", sa.DateTime(), nullable=True),
    )
    _rls("file_table_schemas")

    op.create_table(
        "file_table_rows",
        *_common_cols(),
        sa.Column("kb_id", sa.String(36), nullable=False, index=True),
        sa.Column("sheet_name", sa.String(255), nullable=False),
        sa.Column("row_idx", sa.Integer(), nullable=False),
        sa.Column("values_json", sa.JSON(), nullable=True),
    )
    op.create_index("ix_file_table_rows_kb_sheet", "file_table_rows",
                    ["kb_id", "sheet_name", "row_idx"])
    _rls("file_table_rows")

    op.create_table(
        "identity_matches",
        *_common_cols(),
        sa.Column("object_id", sa.String(36), nullable=False, index=True),
        sa.Column("source_a", sa.JSON(), nullable=True),
        sa.Column("source_b", sa.JSON(), nullable=True),
        sa.Column("source_a_key", sa.String(255), nullable=False),
        sa.Column("source_b_key", sa.String(255), nullable=False),
        sa.Column("pk_a", sa.String(255), nullable=False),
        sa.Column("pk_b", sa.String(255), nullable=False),
        sa.Column("match_tier", sa.String(20), nullable=False),
        sa.Column("normalization_rule_json", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="inferred"),
        sa.Column("evidence_json", sa.JSON(), nullable=True),
        sa.Column("reviewed_by", sa.String(255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("summary_note", sa.Text(), nullable=True),
        sa.UniqueConstraint("org_id", "object_id", "source_a_key", "pk_a",
                            "source_b_key", "pk_b",
                            name="uq_identity_match_pair"),
    )
    op.create_index("ix_identity_matches_status", "identity_matches",
                    ["org_id", "status"])
    _rls("identity_matches")


def downgrade() -> None:
    for t in ("identity_matches", "file_table_rows", "file_table_schemas"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
        op.drop_table(t)
