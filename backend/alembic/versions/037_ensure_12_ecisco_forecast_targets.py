"""ensure 12 ecisco bi forecast targets exist

Revision ID: 037
Revises: 036
Create Date: 2026-08-03

Inserts the 12 Ecisco BI dashboard forecast targets (one per product) for
the default org, if they don't already exist. Idempotent — delegates to
seed_ecisco_forecast_targets which skips existing rows.

Forward-only; no downgrade (targets are data, not schema).
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy.orm import Session
    from app.services.forecasting.seed_ecisco_targets import seed_ecisco_forecast_targets

    conn = op.get_bind()
    session = Session(bind=conn)
    try:
        count = seed_ecisco_forecast_targets(
            session, org_id="default-org", app_id="default-app"
        )
        print(f"037: seeded {count} new ecisco forecast targets (0 = already existed)")
    finally:
        # Don't close the session — alembic owns the connection.
        pass


def downgrade() -> None:
    # Forward-only migration. Targets are data; do not delete on downgrade.
    pass
