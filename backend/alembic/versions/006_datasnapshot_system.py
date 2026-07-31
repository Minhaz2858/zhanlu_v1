"""Phase 4: DataSnapshot system — data_snapshots + snapshot_artifact_links.

Revision ID: 006
Revises: 005
Create Date: 2025-07-12
"""

from alembic import op
import sqlalchemy as sa


revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- data_snapshots ---
    op.create_table(
        "data_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("datasource_id", sa.String(36), nullable=True),
        sa.Column("knowledge_base_id", sa.String(36), nullable=True),
        sa.Column("natural_language", sa.Text, nullable=True),
        sa.Column("sql_query", sa.Text, nullable=False),
        sa.Column("sql_validated", sa.Boolean, server_default=sa.text("true"), nullable=False),
        sa.Column("result_data", sa.JSON, nullable=True),
        sa.Column("result_columns", sa.JSON, nullable=True),
        sa.Column("row_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("data_size_bytes", sa.Integer, server_default="0", nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("snapshot_format", sa.String(20), server_default="json", nullable=False),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=True),
        sa.Column("execution_id", sa.String(36), nullable=True),
        sa.Column("created_by_agent_id", sa.String(36), nullable=True),
        sa.Column("expires_at", sa.DateTime, nullable=True),
        sa.Column("query_duration_ms", sa.Integer, nullable=True),
        sa.Column("metadata_json", sa.JSON, nullable=True),
    )
    op.create_index("ix_data_snapshots_org_id", "data_snapshots", ["org_id"])
    op.create_index("ix_data_snapshots_app_id", "data_snapshots", ["app_id"])
    op.create_index("ix_data_snapshots_datasource_id", "data_snapshots", ["datasource_id"])
    op.create_index("ix_data_snapshots_conversation_id", "data_snapshots", ["conversation_id"])
    op.create_index("ix_data_snapshots_execution_id", "data_snapshots", ["execution_id"])

    # --- snapshot_artifact_links ---
    op.create_table(
        "snapshot_artifact_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("data_snapshots.id"), nullable=False),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifacts.id"), nullable=False),
        sa.Column("artifact_version_id", sa.String(36), sa.ForeignKey("artifact_versions.id"), nullable=True),
        sa.Column("source_part_id", sa.String(36), nullable=True),
        sa.Column("usage_note", sa.String(500), nullable=True),
    )
    op.create_index("ix_snapshot_artifact_links_org_id", "snapshot_artifact_links", ["org_id"])
    op.create_index("ix_snapshot_artifact_links_app_id", "snapshot_artifact_links", ["app_id"])
    op.create_index("ix_snapshot_artifact_links_snapshot_id", "snapshot_artifact_links", ["snapshot_id"])
    op.create_index("ix_snapshot_artifact_links_artifact_id", "snapshot_artifact_links", ["artifact_id"])


def downgrade() -> None:
    op.drop_table("snapshot_artifact_links")
    op.drop_table("data_snapshots")
