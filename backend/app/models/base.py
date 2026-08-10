"""Base model with shared columns: id, created_date, updated_date, created_by_id, is_deleted.

All entity models inherit from TimestampedBase to get these columns automatically.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import String, DateTime, Boolean, inspect, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TimestampedBase(Base):
    """Abstract base providing id, timestamps, ownership, soft-delete, and multi-tenant columns.

    Every model inherits ``org_id`` and ``app_id`` — the isolation wall mandated
    by Layer 1 of the enterprise architecture.  Backward-compatible defaults
    (``"default-org"`` / ``"default-app"``) ensure existing single-tenant data
    continues to work without modification.
    """

    __abstract__ = True

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    created_date: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    updated_date: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )
    created_by_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), default=False, nullable=False
    )
    # --- Multi-tenant isolation wall (Layer 1) ---
    org_id: Mapped[str] = mapped_column(
        String(36), default="default-org", nullable=False, index=True
    )
    app_id: Mapped[str] = mapped_column(
        String(36), default="default-app", nullable=False, index=True
    )

    def to_dict(self) -> dict:
        """Serialize model to dict with ISO-format datetime strings.

        Excludes internal columns (is_deleted, password_hash) from the output
        to match the Base44 API response format expected by the frontend.

        Uses the SQLAlchemy mapper's ``column_attrs`` to resolve the correct
        Python attribute name for each column.  This is critical because
        ``Column.key`` and ``Column.name`` both return the *database* column
        name, which can collide with SQLAlchemy reserved class attributes.
        For example, ``AgentConversation`` has a column named ``metadata``
        (Python attr: ``metadata_``).  ``getattr(self, "metadata")`` would
        return the class-level ``MetaData`` object instead of the column
        value, causing ``jsonable_encoder`` to recurse infinitely
        (MetaData → tables → columns → tables → …) and crash with
        ``RecursionError``.
        """
        result = {}
        mapper = inspect(self.__class__)
        for prop in mapper.column_attrs:
            val = getattr(self, prop.key)
            col_name = prop.columns[0].name
            if isinstance(val, datetime):
                result[col_name] = val.isoformat()
            elif val is None:
                result[col_name] = None
            else:
                result[col_name] = val
        # Remove internal fields from API output
        result.pop("is_deleted", None)
        result.pop("password_hash", None)
        return result
