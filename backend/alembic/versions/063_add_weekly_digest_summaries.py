"""Add market_overview and executive_summary TEXT columns to weekly_report_cards.

Revision ID: 063_add_weekly_digest_summaries
Revises: 062_add_role_description_text
Create Date: 2026-08-14

Adds two nullable TEXT columns to ``weekly_report_cards`` to hold the two
report-level LLM-generated sections that align the weekly summary with the
EDIA C5C9 PPT reference:

- ``market_overview`` — 市场概况 / 原料情况 narrative at the top of the report.
- ``executive_summary`` — 总结预测及建议 numbered recommendations at the bottom.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "063_add_weekly_digest_summaries"
down_revision = "062_add_role_description_text"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    """Return True if ``column`` already exists on ``table``.

    Idempotency guard: the columns were added out-of-band on some deployments
    (the ALTER ran but the alembic version was never stamped), so re-running
    this migration would otherwise fail with a DuplicateColumn error.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_columns(table)}
    return column in existing


def upgrade():
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
    op.drop_column("weekly_report_cards", "executive_summary")
    op.drop_column("weekly_report_cards", "market_overview")
