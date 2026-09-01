"""Artifact service — CRUD, versioning, lifecycle management.

This service is the business logic layer between the artifact router and the
artifact models.  It handles:

* Creating artifacts and versions
* Storing and retrieving blobs with checksum verification
* Lifecycle transitions (draft → building → preview_ready → validated → ...)
* Linking artifacts to chat messages
* Listing artifacts by conversation, type, or status
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.config import settings
from app.models.artifact import (
    Artifact, ArtifactVersion, ArtifactBlob, MessageArtifact,
    ARTIFACT_STATUSES, ARTIFACT_TYPES,
)
from app.services.artifacts.storage import get_blob_storage
from app.services.dashboard_intent import dashboard_intent

logger = logging.getLogger(__name__)


class ArtifactService:
    """Service for managing artifact lifecycle, versioning, and blob storage."""

    def __init__(self, db: Session):
        self.db = db

    # --- Artifact CRUD ---

    def create_artifact(
        self,
        artifact_type: str,
        title: str,
        conversation_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        created_by_agent_id: Optional[str] = None,
        description: Optional[str] = None,
        data_snapshot_ids: Optional[list] = None,
        org_id: str = "default-org",
        app_id: str = "default-app",
        source: str = "agent",
    ) -> Optional[Artifact]:
        """Create a new artifact in 'draft' status.

        Returns the persisted ``Artifact`` on success, or ``None`` if the
        artifact was suppressed by the dashboard-turn guard (see below).

        Dashboard-turn guard (T18): when ``FULLSTACK_DASHBOARD_ENABLED`` is on
        AND the current turn is a dashboard-intent turn (carried via the
        ``dashboard_intent`` ContextVar) AND ``source != "dashboard_app"``, the
        artifact is dropped before it ever reaches the database.  This stops a
        stray analytics-path artifact (e.g. a static "Web page" written from the
        agent's narration sentence) from landing on the same thread as the real
        dashboard app.  The dashboard app itself sets ``source="dashboard_app"``
        so it always passes through.
        """
        if artifact_type not in ARTIFACT_TYPES:
            raise ValueError(f"Invalid artifact_type '{artifact_type}'. Must be one of: {ARTIFACT_TYPES}")

        if (
            settings.FULLSTACK_DASHBOARD_ENABLED
            and dashboard_intent.get()
            and source != "dashboard_app"
        ):
            logger.warning(
                "dropped analytics-path artifact on dashboard turn: "
                "type=%s source=%s title=%r conversation=%s",
                artifact_type, source, title, conversation_id,
            )
            return None

        artifact = Artifact(
            id=str(uuid4()),
            artifact_type=artifact_type,
            title=title,
            description=description,
            status="draft",
            conversation_id=conversation_id,
            execution_id=execution_id,
            created_by_agent_id=created_by_agent_id,
            data_snapshot_ids=data_snapshot_ids or [],
            org_id=org_id,
            app_id=app_id,
            source=source,
        )
        self.db.add(artifact)
        self.db.commit()
        self.db.refresh(artifact)
        logger.info("Created artifact %s (type=%s, title=%s)", artifact.id, artifact_type, title)
        return artifact

    def get_artifact(self, artifact_id: str) -> Optional[Artifact]:
        """Get an artifact by ID (includes versions)."""
        return self.db.query(Artifact).filter(Artifact.id == artifact_id, Artifact.is_deleted == False).first()

    def list_artifacts(
        self,
        conversation_id: Optional[str] = None,
        artifact_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Artifact]:
        """List artifacts with optional filters."""
        query = self.db.query(Artifact).filter(Artifact.is_deleted == False)
        if conversation_id:
            query = query.filter(Artifact.conversation_id == conversation_id)
        if artifact_type:
            query = query.filter(Artifact.artifact_type == artifact_type)
        if status:
            query = query.filter(Artifact.status == status)
        query = query.order_by(Artifact.created_date.desc())
        return query.offset(offset).limit(limit).all()

    def update_status(self, artifact_id: str, new_status: str) -> Optional[Artifact]:
        """Transition an artifact to a new lifecycle status."""
        if new_status not in ARTIFACT_STATUSES:
            raise ValueError(f"Invalid status '{new_status}'. Must be one of: {ARTIFACT_STATUSES}")

        artifact = self.get_artifact(artifact_id)
        if not artifact:
            return None
        artifact.status = new_status
        self.db.commit()
        self.db.refresh(artifact)
        logger.info("Artifact %s status → %s", artifact_id, new_status)
        return artifact

    # --- Version management ---

    def create_version(
        self,
        artifact_id: str,
        changelog: Optional[str] = None,
        source_json: Optional[dict] = None,
        produced_by_skill: Optional[str] = None,
        sandbox_job_id: Optional[str] = None,
    ) -> Optional[ArtifactVersion]:
        """Create a new version of an artifact (version number auto-increments)."""
        artifact = self.get_artifact(artifact_id)
        if not artifact:
            return None

        # Determine next version number
        latest = (
            self.db.query(ArtifactVersion)
            .filter(ArtifactVersion.artifact_id == artifact_id, ArtifactVersion.is_deleted == False)
            .order_by(ArtifactVersion.version_number.desc())
            .first()
        )
        next_num = (latest.version_number + 1) if latest else 1

        version = ArtifactVersion(
            id=str(uuid4()),
            artifact_id=artifact_id,
            version_number=next_num,
            changelog=changelog,
            status="building",
            source_json=source_json,
            produced_by_skill=produced_by_skill,
            sandbox_job_id=sandbox_job_id,
        )
        self.db.add(version)

        # Update artifact's current version pointer and status
        artifact.current_version_id = version.id
        artifact.status = "building"

        self.db.commit()
        self.db.refresh(version)
        logger.info("Created version %d for artifact %s", next_num, artifact_id)
        return version

    def get_versions(self, artifact_id: str) -> list[ArtifactVersion]:
        """List all versions of an artifact."""
        return (
            self.db.query(ArtifactVersion)
            .filter(ArtifactVersion.artifact_id == artifact_id, ArtifactVersion.is_deleted == False)
            .order_by(ArtifactVersion.version_number.desc())
            .all()
        )

    def get_current_version(self, artifact_id: str) -> Optional[ArtifactVersion]:
        """Get the current (latest) version of an artifact."""
        artifact = self.get_artifact(artifact_id)
        if not artifact or not artifact.current_version_id:
            return None
        return (
            self.db.query(ArtifactVersion)
            .filter(ArtifactVersion.id == artifact.current_version_id)
            .first()
        )

    # --- Blob storage ---

    def store_blob(
        self,
        version_id: str,
        blob_type: str,
        file_name: str,
        mime_type: str,
        data: bytes,
    ) -> ArtifactBlob:
        """Store a binary blob (original, preview, or thumbnail) for a version.

        Creates the database row first, then delegates actual byte storage to
        the configured ``BlobStorage`` backend (Postgres or MinIO).  The
        ``storage_uri`` column records where the bytes live.
        """
        checksum = hashlib.sha256(data).hexdigest()
        blob = ArtifactBlob(
            id=str(uuid4()),
            version_id=version_id,
            blob_type=blob_type,
            file_name=file_name,
            mime_type=mime_type,
            file_size=len(data),
            checksum=checksum,
            data=None,  # stored via BlobStorage backend
            storage_uri=None,  # set below after row has an id
        )
        self.db.add(blob)
        self.db.flush()  # populate blob.id so we can build the URI

        # Build URI and persist bytes through the storage backend
        storage = get_blob_storage(self.db)
        blob.storage_uri = f"inline://{blob.id}"
        storage.put(blob.storage_uri, data)

        self.db.commit()
        self.db.refresh(blob)
        logger.info("Stored blob %s (%s, %d bytes, %s)", blob.id, blob_type, len(data), file_name)
        return blob

    def get_blob(self, blob_id: str) -> Optional[ArtifactBlob]:
        """Get blob metadata by ID.  Use ``get_blob_data()`` to read the bytes."""
        return self.db.query(ArtifactBlob).filter(ArtifactBlob.id == blob_id).first()

    def get_blob_data(self, blob: ArtifactBlob) -> Optional[bytes]:
        """Retrieve the binary data for a blob through the storage backend.

        If the blob's ``data`` column is populated (backward-compat / inline),
        return it directly.  Otherwise resolve via ``storage_uri``.
        """
        if blob.data is not None:
            return blob.data
        if not blob.storage_uri:
            return None
        storage = get_blob_storage(self.db)
        return storage.get(blob.storage_uri)

    def get_version_blobs(self, version_id: str, blob_type: Optional[str] = None) -> list[ArtifactBlob]:
        """Get blobs for a version, optionally filtered by type."""
        query = self.db.query(ArtifactBlob).filter(ArtifactBlob.version_id == version_id)
        if blob_type:
            query = query.filter(ArtifactBlob.blob_type == blob_type)
        return query.all()

    def get_preview_blob(self, artifact_id: str) -> Optional[ArtifactBlob]:
        """Get the preview blob (PDF/image) for the current version."""
        version = self.get_current_version(artifact_id)
        if not version:
            return None
        blobs = self.get_version_blobs(version.id, blob_type="preview")
        return blobs[0] if blobs else None

    def get_original_blob(self, artifact_id: str) -> Optional[ArtifactBlob]:
        """Get the original file blob for the current version."""
        version = self.get_current_version(artifact_id)
        if not version:
            return None
        blobs = self.get_version_blobs(version.id, blob_type="original")
        return blobs[0] if blobs else None

    # --- Lifecycle helpers ---

    def mark_version_built(self, version_id: str, validation_report: Optional[dict] = None) -> Optional[ArtifactVersion]:
        """Mark a version as built (preview_ready) with optional validation report."""
        version = self.db.query(ArtifactVersion).filter(ArtifactVersion.id == version_id).first()
        if not version:
            return None

        version.status = "preview_ready"
        version.built_at = datetime.now(timezone.utc)
        version.validation_report = validation_report

        # Update artifact status
        artifact = version.artifact
        if artifact:
            artifact.status = "preview_ready"

        self.db.commit()
        self.db.refresh(version)
        logger.info("Version %s marked as preview_ready", version_id)
        return version

    def mark_version_failed(self, version_id: str, error: str) -> Optional[ArtifactVersion]:
        """Mark a version as failed."""
        version = self.db.query(ArtifactVersion).filter(ArtifactVersion.id == version_id).first()
        if not version:
            return None

        version.status = "failed"
        version.built_at = datetime.now(timezone.utc)
        version.validation_report = {"passed": False, "error": error}

        artifact = version.artifact
        if artifact:
            artifact.status = "failed"

        self.db.commit()
        self.db.refresh(version)
        logger.warning("Version %s failed: %s", version_id, error)
        return version

    # --- Message linking ---

    def link_to_message(
        self,
        artifact_id: str,
        message_id: str,
        conversation_id: str,
        display_order: int = 0,
    ) -> MessageArtifact:
        """Link an artifact to a chat message for inline preview display.

        Idempotent: if a link already exists for the same
        (artifact_id, message_id) pair, the existing row is returned
        unchanged (display_order is NOT updated, no new commit).
        """
        existing = (
            self.db.query(MessageArtifact)
            .filter(
                MessageArtifact.artifact_id == artifact_id,
                MessageArtifact.message_id == message_id,
            )
            .first()
        )
        if existing:
            return existing

        link = MessageArtifact(
            id=str(uuid4()),
            artifact_id=artifact_id,
            message_id=message_id,
            conversation_id=conversation_id,
            display_order=display_order,
        )
        self.db.add(link)
        self.db.commit()
        self.db.refresh(link)
        return link

    def get_message_artifacts(self, message_id: str) -> list[dict]:
        """Get all artifacts linked to a message, with preview info."""
        links = (
            self.db.query(MessageArtifact)
            .filter(MessageArtifact.message_id == message_id)
            .order_by(MessageArtifact.display_order)
            .all()
        )
        results = []
        for link in links:
            artifact = self.get_artifact(link.artifact_id)
            if not artifact:
                continue
            version = self.get_current_version(link.artifact_id)
            preview_blob = self.get_preview_blob(link.artifact_id) if version else None
            original_blob = self.get_original_blob(link.artifact_id) if version else None
            results.append({
                "artifact_id": artifact.id,
                "artifact_type": artifact.artifact_type,
                "title": artifact.title,
                "status": artifact.status,
                "version_number": version.version_number if version else None,
                "has_preview": preview_blob is not None,
                "has_original": original_blob is not None,
                "file_size": original_blob.file_size if original_blob else None,
                "created_date": artifact.created_date.isoformat() if artifact.created_date else None,
            })
        return results
