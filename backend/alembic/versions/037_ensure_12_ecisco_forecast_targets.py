"""ensure 12 ecisco bi forecast targets exist

Revision ID: 037
Revises: 036
Create Date: 2026-08-03

HISTORICAL DATA MIGRATION — now a stamp-only revision on this branch.

Originally inserted 12 Ecisco BI dashboard forecast targets by delegating to
``app.services.forecasting.seed_ecisco_targets``. That module no longer exists:
target seeding was de-hardcoded into ``app/services/forecasting/seed_targets.py``
(``seed_forecast_targets`` / ``discover_and_seed_sku_targets``), which reads the
per-app domain config (``backend/domain_configs`` or ``ZHL_DOMAIN_CONFIG_DIR``)
and seeds ZERO targets when no config carries ``forecast_targets`` — fully
generic behavior. Runtime seeding runs from the nightly forecast task and the
``/bootstrap-nightly-forecast`` endpoint (idempotent).

Kept as a no-op so existing deployments stamped at 037 are untouched (their
target rows were inserted long ago by the historical module) and fresh
databases replay the chain cleanly. Databases that still need targets get them
at runtime when a domain config is present.

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
    # NOTE (fresh-DB remediation): no-op by design — see module docstring.
    # The historical seed module (app.services.forecasting.seed_ecisco_targets)
    # was removed from this branch; target seeding is domain-config-driven at
    # runtime via app.services.forecasting.seed_targets.seed_forecast_targets.
    pass


def downgrade() -> None:
    # Forward-only migration. Targets are data; do not delete on downgrade.
    pass
