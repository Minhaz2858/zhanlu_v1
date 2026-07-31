"""Create forecasting tables for the Ecisco BI Agent.

Tables:
    forecast_targets          — registry of forecastable series
    forecast_runs             — cached forecast results (honesty-gated)
    forecast_accuracy_log     — nightly backtest with per-model MAPE
    forecast_business_rules    — seasonal/causal/event/guardrail rules
    domain_pack_installs      — domain-pack version tracking

All tables follow the ``TimestampedBase`` convention (UUID PK, org_id/app_id
tenant wall, soft-delete, timestamps).  The ``below_naive_baseline`` boolean
on ``forecast_runs`` and ``forecast_accuracy_log`` is the schema-level honesty
gate that prevents silently shipping forecasts worse than a naive baseline.

Revision ID: 034
Revises: 033
Create Date: 2026-07-30
"""

from alembic import op
import sqlalchemy as sa


revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


# ── Base columns shared by every table ──────────────────────────────────────

def _base_columns():
    """Return the list of TimestampedBase columns for op.create_table()."""
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=False),
        sa.Column("updated_date", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column(
            "is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "org_id",
            sa.String(36),
            nullable=False,
            server_default="default-org",
        ),
        sa.Column(
            "app_id",
            sa.String(36),
            nullable=False,
            server_default="default-app",
        ),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    # ── 1. forecast_targets (root — no FK dependencies) ──────────────────
    if "forecast_targets" not in existing:
        op.create_table(
            "forecast_targets",
            *_base_columns(),
            sa.Column("product_key", sa.String(100), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("aliases", sa.JSON(), nullable=True),
            sa.Column("datasource", sa.JSON(), nullable=True),
            sa.Column(
                "level", sa.Integer(), nullable=False, server_default=sa.text("0")
            ),
            sa.Column("quality_grade", sa.String(2), nullable=True),
            sa.Column("quality_stats", sa.JSON(), nullable=True),
            sa.Column(
                "status",
                sa.String(50),
                nullable=False,
                server_default="discovered",
            ),
            sa.Column(
                "source",
                sa.String(50),
                nullable=False,
                server_default="discovery",
            ),
            sa.Column("model_config", sa.JSON(), nullable=True),
            sa.Column(
                "include_in_weekly_report",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column("report_order", sa.Integer(), nullable=True),
        )
        op.create_index(
            "ix_forecast_targets_product_key", "forecast_targets", ["product_key"]
        )
        op.create_index(
            "ix_forecast_targets_status", "forecast_targets", ["status"]
        )
        op.create_unique_constraint(
            "uq_forecast_targets_product_key_org_id",
            "forecast_targets",
            ["product_key", "org_id"],
        )

    # ── 2. forecast_runs (FK → forecast_targets) ─────────────────────────
    if "forecast_runs" not in existing:
        op.create_table(
            "forecast_runs",
            *_base_columns(),
            sa.Column(
                "target_id",
                sa.String(36),
                sa.ForeignKey("forecast_targets.id"),
                nullable=False,
            ),
            sa.Column("results", sa.JSON(), nullable=False),
            sa.Column(
                "below_naive_baseline",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column("confidence", sa.String(50), nullable=True),
            sa.Column("as_of_date", sa.DateTime(), nullable=True),
            sa.Column("model_detail", sa.JSON(), nullable=True),
        )
        op.create_index(
            "ix_forecast_runs_target_id", "forecast_runs", ["target_id"]
        )

    # ── 3. forecast_accuracy_log (FK → forecast_targets) ─────────────────
    if "forecast_accuracy_log" not in existing:
        op.create_table(
            "forecast_accuracy_log",
            *_base_columns(),
            sa.Column(
                "target_id",
                sa.String(36),
                sa.ForeignKey("forecast_targets.id"),
                nullable=False,
            ),
            sa.Column("horizon_days", sa.Integer(), nullable=False),
            sa.Column("n_backtests", sa.Integer(), nullable=True),
            sa.Column("window_start", sa.DateTime(), nullable=True),
            sa.Column("window_end", sa.DateTime(), nullable=True),
            sa.Column("mape", sa.Float(), nullable=True),
            sa.Column("naive_mape", sa.Float(), nullable=True),
            sa.Column("skill_vs_naive", sa.Float(), nullable=True),
            sa.Column(
                "below_naive_baseline",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column("per_model", sa.JSON(), nullable=True),
        )
        op.create_index(
            "ix_forecast_accuracy_log_target_id",
            "forecast_accuracy_log",
            ["target_id"],
        )

    # ── 4. forecast_business_rules (nullable FK → forecast_targets) ──────
    if "forecast_business_rules" not in existing:
        op.create_table(
            "forecast_business_rules",
            *_base_columns(),
            sa.Column(
                "target_id",
                sa.String(36),
                sa.ForeignKey("forecast_targets.id"),
                nullable=True,
            ),
            sa.Column("rule_type", sa.String(50), nullable=False),
            sa.Column("params", sa.JSON(), nullable=False),
            sa.Column(
                "status",
                sa.String(50),
                nullable=False,
                server_default="proposed",
            ),
            sa.Column(
                "source",
                sa.String(50),
                nullable=False,
                server_default="chat",
            ),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("evidence", sa.JSON(), nullable=True),
            sa.Column("approved_by_id", sa.String(36), nullable=True),
            sa.Column("approved_at", sa.DateTime(), nullable=True),
        )
        op.create_index(
            "ix_forecast_business_rules_target_id",
            "forecast_business_rules",
            ["target_id"],
        )
        op.create_index(
            "ix_forecast_business_rules_status",
            "forecast_business_rules",
            ["status"],
        )

    # ── 5. domain_pack_installs (independent — no FK) ────────────────────
    if "domain_pack_installs" not in existing:
        op.create_table(
            "domain_pack_installs",
            *_base_columns(),
            sa.Column("pack_key", sa.String(100), nullable=False),
            sa.Column("pack_version", sa.String(50), nullable=False),
            sa.Column("config", sa.JSON(), nullable=True),
            sa.Column("installed_at", sa.DateTime(), nullable=True),
        )
        op.create_index(
            "ix_domain_pack_installs_pack_key",
            "domain_pack_installs",
            ["pack_key"],
        )


def downgrade() -> None:
    # Drop in reverse dependency order
    op.drop_table("domain_pack_installs")
    op.drop_table("forecast_business_rules")
    op.drop_table("forecast_accuracy_log")
    op.drop_table("forecast_runs")
    op.drop_table("forecast_targets")
