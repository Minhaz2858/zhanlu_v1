"""126_automation_spec_drafts — R2 database foundation for the automation refactor.

Revision ID: 126_automation_spec_drafts
Revises: 125_agent_run_provenance
Create Date: 2026-11-03

WHY
---
Roadmap R2 (docs/plans/2026-11-03-zhanlu-automation-implementation-roadmap.md):
canonical AutomationSpec + server-side draft persistence.

- NEW table ``automation_drafts`` (TS §1.1): server-side draft behind the
  chat review card; the LLM's propose tool creates drafts, only the human
  confirm creates a definition.
- ADD to ``automation_tasks`` (TS §1.2): ``spec`` JSON (optional — legacy
  rows stay valid on the scalar compat columns), ``spec_version`` (default 1),
  ``confirmation_key`` (confirm idempotency key copied from the draft).
- ADD partial unique index ``uq_automation_tasks_org_confirmation_key``
  ``(org_id, confirmation_key) WHERE is_deleted = false`` — the database is
  the confirm idempotency arbiter (TS §4.2). SQLite gets an equivalent
  partial index; both dialects support it natively.

Safe on production data: purely additive, no backfill, no existing column
touched. Idempotency guards (inspector checks) make re-runs after a partial
deployment no-ops instead of hard failures.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "126_automation_spec_drafts"
down_revision: Union[str, Sequence[str], None] = "125_agent_run_provenance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ── 1. automation_drafts (TS §1.1) ──────────────────────────────────
    if "automation_drafts" not in inspector.get_table_names():
        op.create_table(
            "automation_drafts",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
            sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
            sa.Column("created_by_id", sa.String(36), nullable=True),
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_date", sa.DateTime(), nullable=True),
            sa.Column("updated_date", sa.DateTime(), nullable=True),
            sa.Column("spec", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("spec_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
            sa.Column("confirmation_key", sa.String(64), nullable=True),
            sa.Column("origin_session_id", sa.String(36), nullable=True),
            sa.Column("origin_message_id", sa.String(36), nullable=True),
            sa.Column("validation_errors", sa.JSON(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("confirmed_definition_id", sa.String(36), nullable=True),
            sa.Column("draft_note", sa.Text(), nullable=True),
        )
        op.create_index(
            "ix_automation_drafts_org_status", "automation_drafts", ["org_id", "status"]
        )
        op.create_index(
            "ix_automation_drafts_expires_at", "automation_drafts", ["expires_at"]
        )
        op.create_index(
            "ix_automation_drafts_origin_session_id",
            "automation_drafts",
            ["origin_session_id"],
        )

    # ── 2. automation_tasks additive columns (TS §1.2) ──────────────────
    inspector = sa.inspect(bind)
    if not _has_column(inspector, "automation_tasks", "spec"):
        op.add_column("automation_tasks", sa.Column("spec", sa.JSON(), nullable=True))
    if not _has_column(inspector, "automation_tasks", "spec_version"):
        op.add_column(
            "automation_tasks",
            sa.Column("spec_version", sa.Integer(), nullable=False, server_default="1"),
        )
    if not _has_column(inspector, "automation_tasks", "confirmation_key"):
        op.add_column(
            "automation_tasks", sa.Column("confirmation_key", sa.String(64), nullable=True)
        )

    # ── 3. Confirm idempotency arbiter (partial unique index) ───────────
    inspector = sa.inspect(bind)
    existing = {
        idx["name"] for idx in inspector.get_indexes("automation_tasks")
    }
    if "uq_automation_tasks_org_confirmation_key" not in existing:
        if bind.dialect.name == "postgresql":
            op.create_index(
                "uq_automation_tasks_org_confirmation_key",
                "automation_tasks",
                ["org_id", "confirmation_key"],
                unique=True,
                postgresql_where=sa.text("is_deleted = false"),
            )
        else:
            op.create_index(
                "uq_automation_tasks_org_confirmation_key",
                "automation_tasks",
                ["org_id", "confirmation_key"],
                unique=True,
                sqlite_where=sa.text("is_deleted = 0"),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {
        idx["name"] for idx in inspector.get_indexes("automation_tasks")
    }
    if "uq_automation_tasks_org_confirmation_key" in existing:
        op.drop_index("uq_automation_tasks_org_confirmation_key", table_name="automation_tasks")
    inspector = sa.inspect(bind)
    for col in ("confirmation_key", "spec_version", "spec"):
        if _has_column(inspector, "automation_tasks", col):
            op.drop_column("automation_tasks", col)
    if "automation_drafts" in inspector.get_table_names():
        op.drop_table("automation_drafts")
