"""Refresh token model — stores a SHA-256 hash of the raw token, never the raw token itself.

Used by the rotating refresh-token flow (plan 2026-07-27). A refresh token is
issued at login/registration, rotated on each /auth/refresh call (the old token
is marked ``used=True`` and a new one issued), and invalidated en masse on
logout via ``revoke_all_user_refresh_tokens``.
"""
from datetime import datetime

from sqlalchemy import String, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class RefreshToken(TimestampedBase):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
