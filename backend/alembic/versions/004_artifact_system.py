"""Phase 2: Artifact system — artifacts, versions, blobs, message links, source parts.

Revision ID: 004
Revises: 003
Create Date: 2025-07-12
"""

from alembic import op
import sqlalchemy as sa


revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- artifacts ---
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=True),
        sa.Column("execution_id", sa.String(36), nullable=True),
        sa.Column("created_by_agent_id", sa.String(36), nullable=True),
        sa.Column("artifact_type", sa.String(20), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("status", sa.String(20), server_default="draft", nullable=False),
        sa.Column("current_version_id", sa.String(36), nullable=True),
        sa.Column("visibility", sa.String(30), server_default="conversation_private", nullable=False),
        sa.Column("tags", sa.JSON, nullable=True),
        sa.Column("metadata_json", sa.JSON, nullable=True),
        sa.Column("data_snapshot_ids", sa.JSON, nullable=True),
    )
    op.create_index("ix_artifacts_org_id", "artifacts", ["org_id"])
    op.create_index("ix_artifacts_app_id", "artifacts", ["app_id"])
    op.create_index("ix_artifacts_conversation_id", "artifacts", ["conversation_id"])
    op.create_index("ix_artifacts_execution_id", "artifacts", ["execution_id"])

    # --- artifact_versions ---
    op.create_table(
        "artifact_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifacts.id"), nullable=False),
        sa.Column("version_number", sa.Integer, nullable=False),
        sa.Column("changelog", sa.Text, nullable=True),
        sa.Column("status", sa.String(20), server_default="building", nullable=False),
        sa.Column("source_json", sa.JSON, nullable=True),
        sa.Column("validation_report", sa.JSON, nullable=True),
        sa.Column("produced_by_skill", sa.String(100), nullable=True),
        sa.Column("sandbox_job_id", sa.String(36), nullable=True),
        sa.Column("built_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_artifact_versions_org_id", "artifact_versions", ["org_id"])
    op.create_index("ix_artifact_versions_app_id", "artifact_versions", ["app_id"])
    op.create_index("ix_artifact_versions_artifact_id", "artifact_versions", ["artifact_id"])

    # --- artifact_blobs ---
    op.create_table(
        "artifact_blobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("version_id", sa.String(36), sa.ForeignKey("artifact_versions.id"), nullable=False),
        sa.Column("blob_type", sa.String(20), nullable=False),
        sa.Column("file_name", sa.String(500), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("file_size", sa.Integer, nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("data", sa.LargeBinary, nullable=False),
    )
    op.create_index("ix_artifact_blobs_org_id", "artifact_blobs", ["org_id"])
    op.create_index("ix_artifact_blobs_app_id", "artifact_blobs", ["app_id"])
    op.create_index("ix_artifact_blobs_version_id", "artifact_blobs", ["version_id"])

    # --- message_artifacts ---
    op.create_table(
        "message_artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("message_id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifacts.id"), nullable=False),
        sa.Column("display_order", sa.Integer, server_default="0", nullable=False),
    )
    op.create_index("ix_message_artifacts_org_id", "message_artifacts", ["org_id"])
    op.create_index("ix_message_artifacts_app_id", "message_artifacts", ["app_id"])
    op.create_index("ix_message_artifacts_message_id", "message_artifacts", ["message_id"])
    op.create_index("ix_message_artifacts_conversation_id", "message_artifacts", ["conversation_id"])
    op.create_index("ix_message_artifacts_artifact_id", "message_artifacts", ["artifact_id"])

    # --- artifact_source_parts ---
    op.create_table(
        "artifact_source_parts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifacts.id"), nullable=False),
        sa.Column("version_id", sa.String(36), sa.ForeignKey("artifact_versions.id"), nullable=True),
        sa.Column("part_type", sa.String(50), nullable=False),
        sa.Column("part_index", sa.Integer, nullable=False),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("content_json", sa.JSON, nullable=True),
        sa.Column("data_snapshot_id", sa.String(36), nullable=True),
    )
    op.create_index("ix_artifact_source_parts_org_id", "artifact_source_parts", ["org_id"])
    op.create_index("ix_artifact_source_parts_app_id", "artifact_source_parts", ["app_id"])
    op.create_index("ix_artifact_source_parts_artifact_id", "artifact_source_parts", ["artifact_id"])


def downgrade() -> None:
    op.drop_table("artifact_source_parts")
    op.drop_table("message_artifacts")
    op.drop_table("artifact_blobs")
    op.drop_table("artifact_versions")
    op.drop_table("artifacts")
