"""P6 T1 — ontology evolution state + binding staleness.

Revision ID: 112_ontology_evolution
Revises: 111_metric_registry_governance
Create Date: 2026-10-31

ADDITIVE ONLY. ontology_evolution_state is the idempotency ledger: one row
per (org, source) with the last-seen signature — an unchanged source
produces ZERO proposals/revisions/notifications (invariant E1).
ontology_objects gains binding_status (ok|stale|broken) + last_validated_at
— the ONLY fields reconciliation may mutate on an existing object (E2).
"""

from alembic import op
import sqlalchemy as sa

revision = "112_ontology_evolution"
down_revision = "111_metric_registry_governance"
branch_labels = None
depends_on = None


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} USING ("
        "  coalesce(current_setting('app.current_org', true), '') = ''"
        "  OR org_id = current_setting('app.current_org', true)"
        ")"
    )


def upgrade() -> None:
    op.create_table(
        "ontology_evolution_state",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False, index=True),
        sa.Column("app_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("source_key", sa.String(255), nullable=False),
        sa.Column("source_kind", sa.String(30), nullable=False),
        sa.Column("signature", sa.String(64), nullable=False),
        sa.Column("last_status", sa.String(20), nullable=False,
                  server_default=sa.text("'ok'")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("org_id", "source_key",
                            name="uq_evolution_state_org_source"),
    )
    _rls("ontology_evolution_state")

    op.add_column("ontology_objects",
                  sa.Column("binding_status", sa.String(20), nullable=True))
    op.add_column("ontology_objects",
                  sa.Column("last_validated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("ontology_objects", "last_validated_at")
    op.drop_column("ontology_objects", "binding_status")
    op.drop_table("ontology_evolution_state")
