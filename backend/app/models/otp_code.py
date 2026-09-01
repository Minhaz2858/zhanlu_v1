"""OtpCode model — database-backed OTP storage for authentication."""

from datetime import datetime
from sqlalchemy import String, DateTime, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class OtpCode(TimestampedBase):
    __tablename__ = "otp_codes"

    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False)
    # "registration" (verify-account OTP) or "login" (passwordless login code).
    # Isolates the two flows so a registration code cannot be replayed to log in.
    purpose: Mapped[str] = mapped_column(String(20), default="registration", nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
