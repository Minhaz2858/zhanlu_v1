"""Create intelligence tables for EDIA Intelligence Layer integration.

Tables:
    intelligence_events             — detected market events with causal metadata
    intelligence_ingestion_status   — health tracking for background news ingestion

Both tables follow the ``TimestampedBase`` convention (UUID PK, org_id/app_id
tenant wall, soft-delete, timestamps).

Revision ID: 035
Revises: 034
Create Date: 2026-07-31
"""

from alembic import op
import sqlalchemy as sa


revision = "035"
down_revision = "034"
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

    # ── 1. intelligence_events ────────────────────────────────────────────
    if "intelligence_events" not in existing:
        op.create_table(
            "intelligence_events",
            *_base_columns(),
            sa.Column("event_id", sa.String(64), nullable=False, unique=True),
            sa.Column("event_type", sa.String(80), nullable=False),
            sa.Column("headline", sa.Text(), nullable=False),
            sa.Column("affected_commodities", sa.JSON(), nullable=True),
            sa.Column("direction", sa.String(32), nullable=True),
            sa.Column("magnitude_estimate", sa.String(32), nullable=True),
            sa.Column("certainty", sa.String(32), nullable=True),
            sa.Column("time_horizon", sa.String(32), nullable=True),
            sa.Column("geographic_scope", sa.String(64), nullable=True),
            sa.Column("relevance_to_c5_c9", sa.String(32), nullable=True),
            sa.Column("key_entities", sa.JSON(), nullable=True),
            sa.Column("key_information", sa.Text(), nullable=True),
            sa.Column("causal_chain_hint", sa.Text(), nullable=True),
            sa.Column("source_url", sa.Text(), nullable=True),
            sa.Column("source_credibility", sa.String(32), nullable=True),
            sa.Column("full_text", sa.Text(), nullable=True),
            sa.Column("raw_quote", sa.Text(), nullable=True),
            sa.Column("detected_at", sa.String(64), nullable=False),
            sa.Column("impact_magnitude", sa.Float(), nullable=True),
            sa.Column("relevance_score", sa.Float(), nullable=True),
            sa.Column(
                "review_status",
                sa.String(32),
                nullable=False,
                server_default="approved",
            ),
            sa.Column(
                "usefulness_score",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("reviewed_by", sa.String(64), nullable=True),
            sa.Column("reviewed_at", sa.String(64), nullable=True),
            sa.Column("review_note", sa.Text(), nullable=True),
        )
        op.create_index(
            "ix_intelligence_events_event_id", "intelligence_events", ["event_id"]
        )
        op.create_index(
            "ix_intelligence_events_event_type", "intelligence_events", ["event_type"]
        )
        op.create_index(
            "ix_intelligence_events_detected_at", "intelligence_events", ["detected_at"]
        )
        op.create_index(
            "ix_intelligence_events_review_status",
            "intelligence_events",
            ["review_status"],
        )
        op.create_index(
            "ix_intelligence_events_org_id", "intelligence_events", ["org_id"]
        )

    # ── 2. intelligence_ingestion_status ─────────────────────────────────
    if "intelligence_ingestion_status" not in existing:
        op.create_table(
            "intelligence_ingestion_status",
            *_base_columns(),
            sa.Column("agent_name", sa.String(64), nullable=False),
            sa.Column(
                "running",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "cycle_in_progress",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column("scan_interval_seconds", sa.Integer(), nullable=True),
            sa.Column("last_started_at", sa.String(64), nullable=True),
            sa.Column("last_completed_at", sa.String(64), nullable=True),
            sa.Column("last_success_at", sa.String(64), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("last_cycle_duration_ms", sa.Float(), nullable=True),
            sa.Column(
                "last_articles_seen",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "last_events_extracted",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "last_events_stored",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("next_run_at", sa.String(64), nullable=True),
        )
        op.create_index(
            "ix_intelligence_ingestion_status_agent_name",
            "intelligence_ingestion_status",
            ["agent_name"],
        )
        op.create_index(
            "ix_intelligence_ingestion_status_org_id",
            "intelligence_ingestion_status",
            ["org_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "intelligence_ingestion_status" in existing:
        op.drop_table("intelligence_ingestion_status")
    if "intelligence_events" in existing:
        op.drop_table("intelligence_events")
