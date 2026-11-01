"""122_observation_linkage — observed-execution linkage columns.

Revision ID: 122_observation_linkage
Revises: 121_project_org_unit
Create Date: 2026-11-01

WHY
---
Phase 3E: connect ACTUAL execution records (nl2sql_query_logs,
agent_runs) to persisted enterprise business processes with traceable,
honest confidence.

CHANGE (additive only, both tables get the same 4 columns)
-----------------------------------------------------------
- process_id         nullable String(36) + index — matched process
- activity_seq       nullable Integer          — matched activity
- match_confidence   nullable Float            — 0..1 (1.0 = explicit)
- match_method       nullable String(40)       — explicit |
                       ontology_object | tool_binding | lineage

NO hard FK (processes soft-delete; linkage is observational metadata).
NULL = never matched — historical rows are byte-identical and stay
untouched. Recording an observation NEVER promotes process
evidence_status / discovery_status (observed ≠ verified ≠ documented).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "122_observation_linkage"
down_revision: Union[str, Sequence[str], None] = "121_project_org_unit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("nl2sql_query_logs", "agent_runs")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column(
            "process_id", sa.String(length=36), nullable=True))
        op.add_column(table, sa.Column(
            "activity_seq", sa.Integer(), nullable=True))
        op.add_column(table, sa.Column(
            "match_confidence", sa.Float(), nullable=True))
        op.add_column(table, sa.Column(
            "match_method", sa.String(length=40), nullable=True))
        op.create_index(
            f"ix_{table}_process_id", table, ["process_id"])


def downgrade() -> None:
    for table in _TABLES:
        op.drop_index(f"ix_{table}_process_id", table_name=table)
        op.drop_column(table, "match_method")
        op.drop_column(table, "match_confidence")
        op.drop_column(table, "activity_seq")
        op.drop_column(table, "process_id")
