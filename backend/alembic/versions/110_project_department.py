"""P1: department metadata on projects (P1.1, enterprise orchestration).

Departments are LABELS on the existing Project container — no new entity,
per the P1 architecture decision (docs/architecture/
zhanlu-enterprise-agent-discovery-and-routing.md §4.1). The column feeds
agent-discovery scoping ONLY; data authorization is unchanged (per-child
KB bindings + ResourceAccessPolicy + RLS).

Revision ID: 110_project_department
"""
from alembic import op
import sqlalchemy as sa

revision = "110_project_department"
down_revision = "109_document_decision_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspector.get_columns("projects")}
    if "department" not in cols:
        op.add_column(
            "projects",
            sa.Column("department", sa.String(120), nullable=True),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspector.get_columns("projects")}
    if "department" in cols:
        op.drop_column("projects", "department")
