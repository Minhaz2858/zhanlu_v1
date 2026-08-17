"""User model — includes password_hash for authentication (hidden from API output)."""

from typing import Optional

from sqlalchemy import JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class User(TimestampedBase):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="user")
    # Free-text business role descriptions (e.g. "Financial Analyst",
    # "Supply Chain Manager"). Multiple per user. Admin-edited; injected into
    # the agent system prompt for role-aware personalization. Distinct from the
    # binary ``role`` auth column above.
    role_descriptions: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # AI-generated prose description of the user's business role(s). Admin-editable.
    # Generated asynchronously when role_descriptions are saved without an
    # existing description; injected into the agent system prompt alongside
    # role_descriptions for richer personalization. Distinct from the keyword
    # list above (which stays a JSON array of short strings).
    role_description_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
