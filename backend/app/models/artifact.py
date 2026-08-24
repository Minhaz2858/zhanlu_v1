"""Artifact system models — governed, versioned generated outputs.

The artifact system is the core of Zhanlu's output pipeline.  When an agent
generates a PPT, DOCX, MD, HTML, or dashboard, it doesn't just produce a file —
it produces a governed ``Artifact`` with:

* Versioning — every edit creates a new ``ArtifactVersion``
* Blobs — original file + preview PDF + thumbnail stored as ``ArtifactBlob``
* Provenance — links to the conversation, execution, and data snapshots
* Lifecycle — draft → building → preview_ready → validated → approved → published
* Inline preview — permission-checked API, never raw file paths
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, DateTime, Text, LargeBinary, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TimestampedBase


# Valid artifact lifecycle states (in order)
ARTIFACT_STATUSES = [
    "draft",         # Created but not yet built
    "building",      # Generation in progress (sandbox job running)
    "preview_ready",  # Built, preview available, awaiting validation
    "editing",       # User requested changes, new version being built
    "validated",     # Passed automated validation checks
    "approved",      # User / reviewer approved
    "published",     # Published to workspace / shared
    "failed",        # Build or validation failed
    "archived",      # Superseded or manually archived
]

# Valid artifact types
ARTIFACT_TYPES = ["pptx", "docx", "pdf", "md", "html", "html_report", "chart", "dashboard", "mini_app", "image", "xlsx"]


class Artifact(TimestampedBase):
    """A governed generated output (PPT, DOCX, MD, HTML, dashboard, etc.).

    An artifact is the top-level entity.  Each edit creates a new
    ``ArtifactVersion`` which contains the actual file blobs.
    """

    __tablename__ = "artifacts"

    conversation_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    execution_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    created_by_agent_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    artifact_type: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    current_version_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    visibility: Mapped[str] = mapped_column(String(30), default="conversation_private", nullable=False)
    tags: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Evidence lineage — list of DataSnapshot IDs this artifact cites
    data_snapshot_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    canonical_format: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    # Origin of the artifact.  "agent" = normal analytics/tool path;
    # "dashboard_app" = a fullstack dashboard app.  Used by the dashboard-turn
    # guard (T18) to drop stray non-dashboard artifacts on a dashboard turn.
    source: Mapped[str] = mapped_column(String(50), default="agent", nullable=False, index=True)

    versions: Mapped[list["ArtifactVersion"]] = relationship(
        back_populates="artifact", cascade="all, delete-orphan"
    )


class ArtifactVersion(TimestampedBase):
    """A specific version of an artifact (immutable once created).

    Every edit/regeneration creates a new version.  Versions are never
    modified — a new version is created instead.
    """

    __tablename__ = "artifact_versions"

    artifact_id: Mapped[str] = mapped_column(String(36), ForeignKey("artifacts.id"), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    changelog: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="building", nullable=False)
    # Structured source parts for partial regeneration ("edit slide 3")
    source_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Validation report (file opens, sources exist, format checks)
    validation_report: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Skill / sandbox job that produced this version
    produced_by_skill: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    sandbox_job_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    built_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    artifact: Mapped["Artifact"] = relationship(back_populates="versions")
    blobs: Mapped[list["ArtifactBlob"]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )


class ArtifactBlob(TimestampedBase):
    """Binary storage for an artifact version — original, preview, thumbnail.

    Stored as BYTEA in PostgreSQL / LargeBinary in SQLite.  Each version
    can have multiple blobs: the original file, a preview PDF, and thumbnails.
    """

    __tablename__ = "artifact_blobs"

    version_id: Mapped[str] = mapped_column(String(36), ForeignKey("artifact_versions.id"), nullable=False, index=True)
    blob_type: Mapped[str] = mapped_column(String(20), nullable=False)  # original | preview | thumbnail
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA-256
    storage_uri: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    data: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)

    version: Mapped["ArtifactVersion"] = relationship(back_populates="blobs")


class MessageArtifact(TimestampedBase):
    """Links an artifact to a chat message for inline preview display.

    When an agent produces an artifact during a conversation, a
    ``MessageArtifact`` row connects the artifact to the specific message
    so the frontend can render an inline preview card.
    """

    __tablename__ = "message_artifacts"

    message_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    artifact_id: Mapped[str] = mapped_column(String(36), ForeignKey("artifacts.id"), nullable=False, index=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ArtifactSourcePart(TimestampedBase):
    """Structured source part for partial regeneration.

    Instead of regenerating an entire PPT, the system can regenerate
    individual parts (e.g., "slide 3", "chart 2", "section 2 paragraph 1").
    Each part is stored separately so partial edits are surgical.
    """

    __tablename__ = "artifact_source_parts"

    artifact_id: Mapped[str] = mapped_column(String(36), ForeignKey("artifacts.id"), nullable=False, index=True)
    version_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("artifact_versions.id"), nullable=True)
    part_type: Mapped[str] = mapped_column(String(50), nullable=False)  # slide | chart | section | table
    part_index: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    content_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    data_snapshot_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
