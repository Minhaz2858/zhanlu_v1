"""Session state table for the session-cached re-export feature.

Revision ID: 077_add_session_state
Revises: 076_data_execution
Create Date: 2026-08-24

Adds the ``session_states`` table: one row per chat session tracking the most
recent cached data execution plus a monotonic counter. ``session_id`` is the
SOLE primary key — the model overrides the inherited ``TimestampedBase.id``
to a non-PK, nullable column, so this migration deliberately does NOT put
``id`` into the primary key (no composite PK).

Idempotent: table existence check first (project convention).
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "077_add_session_state"
down_revision = "076_data_execution"
branch_labels = None
depends_on = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table in inspector.get_table_names()


def upgrade() -> None:
    if _table_exists("session_states"):
        return
    op.create_table(
        "session_states",
        # Non-PK nullable override of TimestampedBase.id (see model).
        sa.Column("id", sa.String(36), nullable=True),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("last_execution_id", sa.String(64), nullable=True),
        sa.Column("last_tool_name", sa.String(128), nullable=True),
        sa.Column("last_data_signature", sa.String(64), nullable=True),
        sa.Column("execution_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("org_id", sa.String(64), nullable=True),
        sa.Column("app_id", sa.String(64), nullable=True),
        sa.Column(
            "is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_date",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_date",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.PrimaryKeyConstraint("session_id"),
    )


def downgrade() -> None:
    if _table_exists("session_states"):
        op.drop_table("session_states")
