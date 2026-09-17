"""mcp_servers.granted_agents — which agents may use a server's tools

Revision ID: 087_mcp_granted_agents
Revises: 086_workflow_rerun
Create Date: 2026-09-17

Without this column there was nowhere to record that a connected MCP
server's tools had been handed to an agent, so the tools were unreachable
from any agent no matter how the server was configured.

Semantics: NULL or [] means "every agent" — connecting a server is already
an explicit operator action, so hiding its tools behind a second opt-in
per agent was the failure mode being fixed. A non-empty list restricts the
server's tools to those agent names (AgentConversation.agent_name).
"""
from alembic import op
import sqlalchemy as sa

revision = "087_mcp_granted_agents"
down_revision = "086_workflow_rerun"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mcp_servers", sa.Column("granted_agents", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_servers", "granted_agents")
