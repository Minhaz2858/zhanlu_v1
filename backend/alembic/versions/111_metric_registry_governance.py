"""P5 T1 — MetricDefinition authoritative-registry governance fields.

Revision ID: 111_metric_registry_governance
Revises: 110_project_department
Create Date: 2026-10-31

ADDITIVE ONLY: nullable columns + server defaults; no data is rewritten,
no existing column changes. Lifecycle defaults to 'published' so every
pre-existing enforced metric keeps its exact enforcement semantics.
NOTE: chained onto 110_project_department which is (as of this writing)
uncommitted concurrent work — if that revision is reworked, rebase this.
"""

from alembic import op
import sqlalchemy as sa

revision = "111_metric_registry_governance"
down_revision = "110_project_department"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = [
        sa.Column("time_column", sa.String(200), nullable=True),
        sa.Column("dimensions", sa.JSON(), nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        sa.Column("zero_denominator_behavior", sa.String(20), nullable=True),
        sa.Column("filters_json", sa.JSON(), nullable=True),
        sa.Column("evidence_status", sa.String(20), nullable=False,
                  server_default=sa.text("'verified'")),
        sa.Column("version", sa.Integer(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("lifecycle", sa.String(20), nullable=False,
                  server_default=sa.text("'published'")),
        sa.Column("reviewed_by", sa.String(120), nullable=True),
        sa.Column("source_object_refs", sa.JSON(), nullable=True),
    ]
    for c in cols:
        op.add_column("metric_definitions", c)
    op.create_index(
        "ix_metric_definitions_lifecycle", "metric_definitions",
        ["lifecycle"])


def downgrade() -> None:
    op.drop_index("ix_metric_definitions_lifecycle",
                  table_name="metric_definitions")
    for name in ("source_object_refs", "reviewed_by", "lifecycle",
                 "version", "evidence_status", "filters_json",
                 "zero_denominator_behavior", "currency", "dimensions",
                 "time_column"):
        op.drop_column("metric_definitions", name)
