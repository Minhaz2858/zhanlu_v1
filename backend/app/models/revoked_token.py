"""Revoked access-token JTI blacklist (logout / session kill).

When a user logs out (POST /auth/logout), the JTI of their current access
token is inserted here so subsequent requests presenting that token are
rejected by ``auth_service.verify_token`` (which checks this table). Rows
are kept only until the original token's ``exp`` — a periodic janitor could
prune expired rows, but they're cheap and the table stays small.
"""
from datetime import datetime

from sqlalchemy import String, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class RevokedToken(TimestampedBase):
    __tablename__ = "revoked_tokens"

    jti: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
