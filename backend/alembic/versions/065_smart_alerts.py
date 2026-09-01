"""Smart Alerts — adaptive thresholds + user preferences + webhook.

Revision ID: 065_smart_alerts
Revises: 064_business_semantic_layer
Create Date: 2026-08-18

Adds:
- 7 new ``user_settings`` columns:
  - ``alert_min_severity`` (VARCHAR(10), default 'low')
  - ``alert_muted_products`` (JSON, default '[]')
  - ``alert_channels`` (JSON, default '["in_app"]')
  - ``alert_threshold_multiplier`` (FLOAT, default 1.0)
  - ``alert_quiet_hours`` (JSON, nullable)
  - ``alert_recommendations_enabled`` (BOOLEAN, default TRUE)
  - ``alert_webhook_secret`` (VARCHAR(64), nullable)
- ``alert_product_baselines`` table — cached LLM-judged weekly volatility
  per product (7-day TTL, read-through).

Idempotent: column/table existence checks first (project convention).
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "065_smart_alerts"
down_revision = "064_business_semantic_layer"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_columns(table)}
    return column in existing


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table in inspector.get_table_names()


def upgrade() -> None:
    # ── user_settings: 7 additive preference columns ────────────────────
    # NOTE: string defaults must be SQL string literals ('low', '[]'), not
    # bare identifiers — psycopg2 rejects "DEFAULT low" as a column reference.
    columns = [
        ("alert_min_severity", sa.String(10), "'low'"),
        ("alert_muted_products", sa.JSON(), "'[]'"),
        ("alert_channels", sa.JSON(), "'[\"in_app\"]'"),
        ("alert_threshold_multiplier", sa.Float(), "1.0"),
        ("alert_quiet_hours", sa.JSON(), None),
        ("alert_recommendations_enabled", sa.Boolean(), "true"),
        ("alert_webhook_secret", sa.String(64), None),
    ]
    for name, col_type, default in columns:
        if _column_exists("user_settings", name):
            continue
        kw = {}
        if default is not None:
            kw["server_default"] = sa.text(default) if isinstance(default, str) else default
        op.add_column("user_settings", sa.Column(name, col_type, nullable=True, **kw))

    # ── alert_product_baselines: cached per-product volatility baselines ─
    if not _table_exists("alert_product_baselines"):
        op.create_table(
            "alert_product_baselines",
            sa.Column("product_id", sa.String(64), primary_key=True),
            sa.Column("baseline_pct", sa.Float(), nullable=False),
            sa.Column("reasoning", sa.Text(), nullable=True),
            sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
            sa.Column("model_used", sa.String(64), nullable=True),
        )


def downgrade() -> None:
    for name, _col_type, _default in [
        ("alert_min_severity", None, None),
        ("alert_muted_products", None, None),
        ("alert_channels", None, None),
        ("alert_threshold_multiplier", None, None),
        ("alert_quiet_hours", None, None),
        ("alert_recommendations_enabled", None, None),
        ("alert_webhook_secret", None, None),
    ]:
        if _column_exists("user_settings", name):
            op.drop_column("user_settings", name)

    if _table_exists("alert_product_baselines"):
        op.drop_table("alert_product_baselines")
