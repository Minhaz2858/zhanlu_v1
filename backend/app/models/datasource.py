"""Datasource — connection definition for database adapters.

Stores connection configuration (encrypted), a cached schema snapshot,
and connection controls (max rows, timeout, enabled flag).  The engine
field determines which adapter (SQLite, Postgres) is used at runtime.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, Boolean, DateTime, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase

DATASOURCE_ENGINES = ["sqlite", "postgres", "mysql", "mssql", "bigquery", "snowflake"]
CONNECTION_STATUSES = ["unknown", "connected", "failed", "disabled"]


class Datasource(TimestampedBase):
    """A datasource connection — engine + config + schema snapshot."""

    __tablename__ = "datasources"

    # Identity
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    engine: Mapped[str] = mapped_column(String(30), nullable=False)

    # Connection
    connection_config: Mapped[dict] = mapped_column(JSON, nullable=False)
    connection_status: Mapped[str] = mapped_column(String(20), default="unknown", nullable=False)
    last_connected_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Schema snapshot (cached)
    schema_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    schema_refreshed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Controls
    max_rows_per_query: Mapped[int] = mapped_column(Integer, default=10000, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
