"""121_project_org_unit — authoritative department link on projects.

Revision ID: 121_project_org_unit
Revises: 120_semantic_integration
Create Date: 2026-11-01

WHY
---
Phase 1B.3: connect My Space Projects to the authoritative enterprise
organizational model (EBPM OrgUnit). ``projects.department`` remains a
free-text LABEL (agent-discovery routing only); this adds an optional,
validated link to a real OrgUnit row (unit_type department | team).

CHANGE (additive only)
----------------------
``projects.department_org_unit_id`` — nullable String(36) + index. Plain
id, NO hard FK: org consistency and unit_type are validated by the
entity API when linking (a hard FK would couple cleanup paths and reject
soft-deleted units). No backfill, no auto-assignment — existing projects
keep their rows and labels untouched, and the column stays NULL unless a
human explicitly links a unit.

Authorization note: this column is metadata only. Project ownership and
ResourceShare semantics are untouched; a department association never
grants datasource/KB/agent permissions.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "121_project_org_unit"
down_revision: Union[str, Sequence[str], None] = "120_semantic_integration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("department_org_unit_id", sa.String(36), nullable=True),
    )
    op.create_index(
        "ix_projects_department_org_unit_id", "projects",
        ["department_org_unit_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_projects_department_org_unit_id", table_name="projects")
    op.drop_column("projects", "department_org_unit_id")
