"""Add schema column to knowledge_bases for non-default database schema scoping.

Revision ID: 054
Revises: 053
Create Date: 2026-08-13

The ``schema`` column lets a PostgreSQL/MySQL knowledge base target a
non-default schema (e.g. a data-warehouse schema other than ``public``).
Nullable so existing rows migrate without failing; adapters/connectors
default to ``public`` (Postgres) or the database default (MySQL) when unset.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "054_add_schema_to_knowledge_base"
down_revision = "053_add_alert_payload"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "knowledge_bases",
        sa.Column(
            "schema",
            sa.String(255),
            nullable=True,
            comment="Target database schema (default public for Postgres)",
        ),
    )


def downgrade():
    op.drop_column("knowledge_bases", "schema")
