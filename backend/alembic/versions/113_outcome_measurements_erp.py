"""EBOM production readiness — outcome measurements + ERP submission ledger.

Revision ID: 113_outcome_measurements_erp
Revises: 112_ontology_evolution
Create Date: 2026-10-31

ADDITIVE ONLY. Two new org-scoped tables (RLS enforced):
- outcome_measurements: expected → measured → verified impact. Baseline and
  observed values are governed computations (MetricDefinition), never claims.
- erp_submissions: connector-neutral write-back ledger with an idempotency
  key — duplicate execution is impossible at the storage layer.
"""

from alembic import op
import sqlalchemy as sa

revision = "113_outcome_measurements_erp"
down_revision = "112_ontology_evolution"
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


def _base_cols() -> list:
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False, index=True),
        sa.Column("app_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "outcome_measurements",
        *_base_cols(),
        sa.Column("approval_id", sa.String(36), nullable=True, index=True),
        sa.Column("action_id", sa.String(36), nullable=True),
        sa.Column("escalation_id", sa.String(36), nullable=True),
        sa.Column("draft_id", sa.String(36), nullable=True, index=True),
        sa.Column("metric_definition_id", sa.String(36), nullable=False),
        sa.Column("metric_name", sa.String(255), nullable=False),
        sa.Column("direction", sa.String(10), nullable=False,
                  server_default=sa.text("'increase'")),
        sa.Column("expected_impact", sa.Float(), nullable=True),
        sa.Column("baseline_value", sa.Float(), nullable=True),
        sa.Column("observed_value", sa.Float(), nullable=True),
        sa.Column("baseline_period", sa.String(20), nullable=False),
        sa.Column("observed_period", sa.String(20), nullable=False),
        sa.Column("unit", sa.String(30), nullable=True),
        sa.Column("realized_impact", sa.Float(), nullable=True),
        sa.Column("measurement_status", sa.String(20), nullable=False,
                  server_default=sa.text("'pending'")),
        sa.Column("measurement_note", sa.Text(), nullable=True),
        sa.Column("verification_status", sa.String(20), nullable=False,
                  server_default=sa.text("'unverified'")),
        sa.Column("verified_by", sa.String(36), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("verification_note", sa.Text(), nullable=True),
        sa.Column("evidence_refs", sa.JSON(), nullable=True),
        sa.UniqueConstraint(
            "org_id", "approval_id", "metric_definition_id",
            "baseline_period", "observed_period",
            name="uq_outcome_decision_metric_periods",
        ),
    )
    _rls("outcome_measurements")

    op.create_table(
        "erp_submissions",
        *_base_cols(),
        sa.Column("draft_id", sa.String(36), nullable=False, index=True),
        sa.Column("connector_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default=sa.text("'executing'")),
        sa.Column("external_ref", sa.String(64), nullable=True),
        sa.Column("request_json", sa.JSON(), nullable=True),
        sa.Column("response_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("approval_id", sa.String(36), nullable=True, index=True),
        sa.UniqueConstraint("org_id", "idempotency_key",
                            name="uq_erp_submission_idempotency"),
    )
    _rls("erp_submissions")


def downgrade() -> None:
    op.drop_table("erp_submissions")
    op.drop_table("outcome_measurements")
