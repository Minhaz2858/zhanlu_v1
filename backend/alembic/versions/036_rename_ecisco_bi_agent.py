"""Rename orchestrator_agent to ecisco_bi_assistant

Revision ID: 036
Revises: 035
Create Date: 2026-08-03

One-time idempotent rename: updates any existing orchestrator_agent rows
in the agent_apps table to ecisco_bi_assistant.  Also updates the project
description text to reflect the expanded agent portfolio.

ensure_system_agents() also handles this rename on every startup
(dual safety net), so the migration does NOT fail if no old rows exist.
"""

revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None

from alembic import op


def upgrade():
    # Rename agent_apps rows
    op.execute(
        "UPDATE agent_apps SET name = 'ecisco_bi_assistant' "
        "WHERE name = 'orchestrator_agent' AND is_deleted = false"
    )

    # Update project description to reflect full agent portfolio
    op.execute(
        "UPDATE projects SET description = "
        "'Ecisco BI — EDIA pipeline for C5/C9 petrochemical "
        "domain intelligence. Contains Ecisco BI Assistant, "
        "Perception, RAG Research, Diagnosis, Forecasting, "
        "Pricing, Decision, Knowledge Graph, Weekly Report, "
        "ERP Writeback, and Report Generation agents.' "
        "WHERE name = 'Ecisco BI' AND is_deleted = false"
    )


def downgrade():
    op.execute(
        "UPDATE agent_apps SET name = 'orchestrator_agent' "
        "WHERE name = 'ecisco_bi_assistant' AND is_deleted = false"
    )
    op.execute(
        "UPDATE projects SET description = "
        "'Ecisco BI — EDIA pipeline for C5/C9 petrochemical "
        "domain intelligence. Contains Orchestrator, "
        "Perception, RAG Research, Diagnosis, Forecasting, "
        "Pricing, Data, and Report Generation agents.' "
        "WHERE name = 'Ecisco BI' AND is_deleted = false"
    )
