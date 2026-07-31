"""DataSnapshot model — immutable, checksummed query result evidence.

The DataSnapshot is the evidence layer for data-driven artifacts.  The
architecture is adamant: data-driven artifacts MUST cite immutable
DataSnapshots, not live mutable queries.  The flow is:

    NL2SQL → SQL validation → read-only query → DataSnapshot (immutable) → artifact cites snapshot ID

This ensures that when a PPT cites a chart showing Q2 revenue, the underlying
data is verifiable, reproducible, and tamper-proof.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, DateTime, Text, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


# Valid snapshot formats
SNAPSHOT_FORMATS = ["json", "csv", "parquet"]

# Valid snapshot statuses
SNAPSHOT_STATUSES = ["active", "expired", "archived"]


class DataSnapshot(TimestampedBase):
    """An immutable snapshot of query results — the evidence layer for artifacts.

    Once created, a DataSnapshot is never modified.  It records:
    - The natural language question that triggered it
    - The generated SQL
    - The query result data (as JSON)
    - A SHA-256 checksum for integrity verification
    - Links to the datasource it was queried from
    - Column metadata (types, names) for downstream rendering
    """

    __tablename__ = "data_snapshots"

    # Source
    datasource_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    knowledge_base_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)

    # Query details
    natural_language: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sql_query: Mapped[str] = mapped_column(Text, nullable=False)
    sql_validated: Mapped[bool] = mapped_column(default=True, nullable=False)

    # Result data
    result_data: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # List of row dicts
    result_columns: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # Column metadata
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    data_size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Integrity
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA-256 of result_data
    snapshot_format: Mapped[str] = mapped_column(String(20), default="json", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)

    # Context
    conversation_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    execution_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    created_by_agent_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # Expiration (snapshots can expire but are never deleted — they're archived)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Metadata
    query_duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class SnapshotArtifactLink(TimestampedBase):
    """Links a DataSnapshot to an Artifact — evidence lineage.

    When an artifact cites a DataSnapshot, this link records which snapshot
    was used for which artifact (and optionally which specific part of the
    artifact, e.g., "slide 3" or "chart 2").
    """

    __tablename__ = "snapshot_artifact_links"

    snapshot_id: Mapped[str] = mapped_column(String(36), ForeignKey("data_snapshots.id"), nullable=False, index=True)
    artifact_id: Mapped[str] = mapped_column(String(36), ForeignKey("artifacts.id"), nullable=False, index=True)
    artifact_version_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("artifact_versions.id"), nullable=True)
    source_part_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)  # Link to specific ArtifactSourcePart
    usage_note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)  # e.g., "Used for revenue chart on slide 3"
