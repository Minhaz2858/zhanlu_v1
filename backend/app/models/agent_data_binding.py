"""AgentDataBinding — links an agent to a datasource with access controls.

Specifies which datasource an agent can access, which tables/columns,
and whether read or read-write access is granted.  Defaults to read-only.
"""

from typing import Optional

from sqlalchemy import String, Boolean, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class AgentDataBinding(TimestampedBase):
    """A data binding between an agent and a datasource.

    Enforces the principle of least privilege: agents only get access
    to the specific tables/columns they need, read-only by default.
    """

    __tablename__ = "agent_data_bindings"

    agent_app_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_apps.id"), nullable=False, index=True)
    datasource_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    # Access controls
    access_mode: Mapped[str] = mapped_column(String(20), default="read_only", nullable=False)  # read_only | read_write
    allowed_tables: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # None = all tables
    allowed_columns: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # {table: [columns]}
    blocked_tables: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Row-level filters (e.g., {"department": "sales"})
    row_filters: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Whether NL2SQL is allowed for this binding
    nl2sql_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
