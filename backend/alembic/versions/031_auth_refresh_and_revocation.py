"""031_auth_refresh_and_revocation

Revision ID: 031
Revises: 030
Create Date: 2026-07-27

Adds the ``refresh_tokens`` and ``revoked_tokens`` tables for the rotating
refresh-token + access-token-revocation flow (plan 2026-07-27).

  * refresh_tokens  — one row per issued refresh token; stores a SHA-256 hash
                       of the raw token (never the raw token), an expiry, and a
                       ``used`` flag rotated on each /auth/refresh call.
  * revoked_tokens  — JTI blacklist populated on /auth/logout so the access
                       token is rejected by ``auth_service.verify_token``.

Both tables inherit the full ``TimestampedBase`` column set (id, timestamps,
created_by_id, is_deleted, org_id, app_id) to match their SQLAlchemy models.
Idempotent in live mode (skips creation if the table already exists).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "031"
down_revision: Union[str, None] = "030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(conn, table: str) -> bool:
    """Live-mode table-existence check. Always False in offline (--sql) mode."""
    if op.get_context().as_sql:
        return False
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = :t"
        ),
        {"t": table},
    ).fetchone()
    return result is not None


def _base_columns():
    """Mirror app.models.base.TimestampedBase (incl. multi-tenant org_id/app_id)."""
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
    ]


def upgrade() -> None:
    conn = op.get_bind()

    if not _table_exists(conn, "refresh_tokens"):
        op.create_table("refresh_tokens", *_base_columns(),
            sa.Column("user_id", sa.String(36), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("expires_at", sa.DateTime, nullable=False),
            sa.Column("used", sa.Boolean, nullable=False, server_default=sa.text("false")),
        )
        op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
        op.create_index("ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"], unique=True)

    if not _table_exists(conn, "revoked_tokens"):
        op.create_table("revoked_tokens", *_base_columns(),
            sa.Column("jti", sa.String(36), nullable=False),
            sa.Column("user_id", sa.String(36), nullable=False),
            sa.Column("expires_at", sa.DateTime, nullable=False),
        )
        op.create_index("ix_revoked_tokens_jti", "revoked_tokens", ["jti"], unique=True)
        op.create_index("ix_revoked_tokens_user_id", "revoked_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_table("revoked_tokens")
    op.drop_table("refresh_tokens")
