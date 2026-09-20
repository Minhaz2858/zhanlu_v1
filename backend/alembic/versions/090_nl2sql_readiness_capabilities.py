"""090_nl2sql_readiness_capabilities

Revision ID: 090_nl2sql_readiness
Revises: 089_harness_rule_events
Create Date: 2026-09-20

P1 of the dynamic database semantic-enrichment work:

1. ``knowledge_bases.nl2sql_state`` / ``nl2sql_state_reason`` /
   ``nl2sql_state_at`` — the source lifecycle
   (connected → discovering → indexed → enriched → validated → nl2sql_ready,
   with discovery_failed / enrichment_partial / validation_failed branches).
   Empty string means "never staged on this build"; the readiness gate then
   derives an honest state from ``catalog_status`` instead of assuming ready.

2. ``kb_capability_profiles`` — one discovered capability profile per
   connected source (dialect, identifier quoting, date/timestamp functions,
   pagination, null behavior, read-only, view + window-function support,
   timezone/locale, limits, catalog version, probe evidence).

Both are source-scoped and dialect-agnostic: no customer, table, schema or
vendor-specific value is written by this migration.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "090_nl2sql_readiness"
down_revision: Union[str, None] = "089_harness_rule_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. source lifecycle on knowledge_bases ────────────────────────────
    op.add_column(
        "knowledge_bases",
        sa.Column("nl2sql_state", sa.String(24), nullable=False, server_default=""),
    )
    op.add_column(
        "knowledge_bases",
        sa.Column("nl2sql_state_reason", sa.Text, nullable=True),
    )
    op.add_column(
        "knowledge_bases",
        sa.Column("nl2sql_state_at", sa.DateTime, nullable=True),
    )
    op.create_index(
        "ix_knowledge_bases_nl2sql_state", "knowledge_bases", ["nl2sql_state"]
    )

    # ── 2. capability profile table ───────────────────────────────────────
    op.create_table(
        "kb_capability_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "kb_id",
            sa.String(36),
            sa.ForeignKey("knowledge_bases.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("dialect", sa.String(32), nullable=False, server_default=""),
        sa.Column("db_type_raw", sa.String(64), nullable=True),
        sa.Column("server_version", sa.String(255), nullable=True),
        sa.Column("quote_style", sa.String(16), nullable=True),
        sa.Column("quote_open", sa.String(4), nullable=True),
        sa.Column("quote_close", sa.String(4), nullable=True),
        sa.Column("pagination", sa.String(16), nullable=True),
        sa.Column("null_function", sa.String(32), nullable=True),
        sa.Column("concat_template", sa.String(128), nullable=True),
        sa.Column("supports_views", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "supports_window_functions",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "supports_explain", sa.Boolean, nullable=False, server_default=sa.text("false")
        ),
        sa.Column("read_only", sa.Boolean, nullable=True),
        sa.Column("catalogs", sa.JSON, nullable=True),
        sa.Column("schemas", sa.JSON, nullable=True),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("locale", sa.String(64), nullable=True),
        sa.Column("query_timeout_s", sa.Integer, nullable=True),
        sa.Column("max_rows", sa.Integer, nullable=True),
        sa.Column("connect_latency_ms", sa.Float, nullable=True),
        sa.Column("capabilities", sa.JSON, nullable=True),
        sa.Column("probe_evidence", sa.JSON, nullable=True),
        sa.Column("catalog_version", sa.String(64), nullable=True, index=True),
        sa.Column("table_count", sa.Integer, nullable=True),
        sa.Column(
            "detected_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("detection_error", sa.Text, nullable=True),
        # TimestampedBase columns
        sa.Column(
            "created_date",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_date",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org", index=True),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app", index=True),
        sa.UniqueConstraint("kb_id", name="uq_kb_capability_profile_kb"),
    )


def downgrade() -> None:
    op.drop_table("kb_capability_profiles")
    op.drop_index("ix_knowledge_bases_nl2sql_state", table_name="knowledge_bases")
    op.drop_column("knowledge_bases", "nl2sql_state_at")
    op.drop_column("knowledge_bases", "nl2sql_state_reason")
    op.drop_column("knowledge_bases", "nl2sql_state")
