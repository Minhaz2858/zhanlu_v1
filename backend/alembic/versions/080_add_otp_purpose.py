"""Add `purpose` discriminator to otp_codes so login and registration codes are isolated.

Revision ID: 080_add_otp_purpose
Revises: 079_chat_messages_sources
Create Date: 2026-08-31
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "080_add_otp_purpose"
down_revision = "079_chat_messages_sources"
branch_labels = None
depends_on = None


def upgrade():
    # Discriminate OTP codes by intent. Existing rows default to "registration"
    # so they keep working with the unchanged registration flow.
    op.add_column(
        "otp_codes",
        sa.Column(
            "purpose",
            sa.String(20),
            nullable=False,
            server_default="registration",
        ),
    )
    # Speed up (email, purpose) lookups used by verify_otp.
    op.create_index(
        "ix_otp_codes_email_purpose",
        "otp_codes",
        ["email", "purpose"],
    )


def downgrade():
    op.drop_index("ix_otp_codes_email_purpose", table_name="otp_codes")
    op.drop_column("otp_codes", "purpose")
