"""ArtifactBuildManifest — build metadata for artifact generation.

Connects an artifact to a template asset, build parameters, and the
sandbox job that produced it.  Tracks validation results, checksums,
output file size, timing, and error messages.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, DateTime, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase

BUILD_STATUSES = ["pending", "building", "completed", "failed", "cancelled", "validating"]


class ArtifactBuildManifest(TimestampedBase):
    """Build manifest — template ref + params → artifact version."""

    __tablename__ = "artifact_build_manifests"

    # References
    artifact_id: Mapped[str] = mapped_column(String(36), nullable=False)
    artifact_version_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    execution_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # Build spec
    build_type: Mapped[str] = mapped_column(String(30), nullable=False)
    template_asset_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    template_checksum: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    build_params: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    data_snapshot_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Build result
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    sandbox_job_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    output_checksum: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    output_file_name: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    output_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timing
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Validation
    validation_report: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
