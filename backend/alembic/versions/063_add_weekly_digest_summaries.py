"""Add market_overview and executive_summary TEXT columns to weekly_report_cards.

Revision ID: 063_add_weekly_digest_summaries
Revises: 062_add_role_description_text
Create Date: 2026-08-14

Adds two nullable TEXT columns to ``weekly_report_cards`` to hold the two
report-level LLM-generated sections that align the weekly summary with the
EDIA C5C9 PPT reference:

- ``market_overview`` — 市场概况 / 原料情况 narrative at the top of the report.
- ``executive_summary`` — 总结预测及建议 numbered recommendations at the bottom.

This migration is the EXPLICIT owner of the ``weekly_report_cards`` table
(previously only materialised by ``create_all`` at app startup, which broke a
fresh ``alembic upgrade head`` because 089 ran after this migration). If the
table is absent it is created here with the complete column set (including the
two summary columns); otherwise only the missing summary columns are added.
Both paths are existence-guarded so the migration is idempotent.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "063_add_weekly_digest_summaries"
down_revision = "062_add_role_description_text"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    """Return True if ``column`` already exists on ``table`` (idempotency guard)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    try:
        existing = {c["name"] for c in inspector.get_columns(table)}
    except sa.exc.NoSuchTableError:
        return False
    return column in existing


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table in inspector.get_table_names()


def _ts_columns() -> list:
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("org_id", sa.String(36), nullable=False, index=True),
        sa.Column("app_id", sa.String(36), nullable=False, index=True),
    ]


def _create_weekly_report_cards() -> None:
    op.create_table(
        "weekly_report_cards",
        *_ts_columns(),
        sa.Column("week_start", sa.Date(), nullable=False, index=True),
        sa.Column("week_end", sa.Date(), nullable=False),
        sa.Column("title_zh", sa.String(200), nullable=False),
        sa.Column("title_en", sa.String(200), nullable=False),
        sa.Column("product_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_msg", sa.Text(), nullable=True),
        sa.Column("market_overview", sa.Text(), nullable=True),
        sa.Column("executive_summary", sa.Text(), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=True),
        sa.Index("ix_weekly_report_cards_org_week", "org_id", "week_start", unique=True),
    )


def upgrade():
    if not _table_exists("weekly_report_cards"):
        _create_weekly_report_cards()
        return
    if not _column_exists("weekly_report_cards", "market_overview"):
        op.add_column(
            "weekly_report_cards",
            sa.Column("market_overview", sa.Text(), nullable=True),
        )
    if not _column_exists("weekly_report_cards", "executive_summary"):
        op.add_column(
            "weekly_report_cards",
            sa.Column("executive_summary", sa.Text(), nullable=True),
        )


def downgrade():
    if not _table_exists("weekly_report_cards"):
        return
    op.drop_column("weekly_report_cards", "executive_summary")
    op.drop_column("weekly_report_cards", "market_overview")
