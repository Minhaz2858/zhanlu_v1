"""mcp_servers 2026-spec columns

Revision ID: 084_mcp_servers_columns
Revises: 083_workflow_runs
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa

revision = "084_mcp_servers_columns"
down_revision = "083_workflow_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mcp_servers", sa.Column("base_url", sa.String(500), nullable=True))
    op.add_column("mcp_servers", sa.Column("auth_type", sa.String(50), nullable=True, server_default="none"))
    op.add_column("mcp_servers", sa.Column("capabilities", sa.JSON(), nullable=True))
    op.add_column("mcp_servers", sa.Column("last_probed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("mcp_servers", sa.Column("oauth_token_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_servers", "oauth_token_json")
    op.drop_column("mcp_servers", "last_probed_at")
    op.drop_column("mcp_servers", "capabilities")
    op.drop_column("mcp_servers", "auth_type")
    op.drop_column("mcp_servers", "base_url")
