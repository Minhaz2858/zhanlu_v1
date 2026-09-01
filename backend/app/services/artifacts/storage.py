"""BlobStorage abstraction — pluggable backends for artifact blob persistence.

Provides an abstract interface ``BlobStorage`` and two concrete implementations:

* ``PostgresBlobStorage`` — stores blobs inline in the ``data`` column of
  ``artifact_blobs``.  URIs use the ``inline://`` scheme.
* ``MinioBlobStorage`` — stores blobs in MinIO (S3-compatible object storage).
  URIs use the ``s3://`` scheme.  Requires ``minio>=7.0``.
"""

from __future__ import annotations

import abc
import hashlib
import io
import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


# ── Abstract interface ──────────────────────────────────────────────────────


class BlobStorage(abc.ABC):
    """Abstract interface for artifact blob persistence.

    Each backend is identified by a URI scheme used in ``storage_uri``
    (``inline://`` for Postgres, ``s3://`` for MinIO).  The URI is the
    canonical pointer — callers never need to know which backend is behind it.
    """

    @abc.abstractmethod
    def put(self, uri: str, data: bytes) -> None:
        """Store ``data`` at the given ``uri``."""

    @abc.abstractmethod
    def get(self, uri: str) -> Optional[bytes]:
        """Retrieve data from ``uri``, or None if not found."""

    @abc.abstractmethod
    def delete(self, uri: str) -> bool:
        """Delete the blob at ``uri``.  Returns True if something was deleted."""

    @abc.abstractmethod
    def exists(self, uri: str) -> bool:
        """Return True if ``uri`` points to existing data."""


# ── PostgresBlobStorage ─────────────────────────────────────────────────────


class PostgresBlobStorage(BlobStorage):
    """Store blobs inline in the ``artifact_blobs.data`` column.

    This is the default backend (``ARTIFACT_STORAGE_BACKEND="postgres_bytea"``).
    URIs use the ``inline://<blob_id>`` scheme — the blob_id maps directly to
    the ``artifact_blobs.id`` row where ``data`` is stored.
    """

    SCHEME = "inline"

    def __init__(self, db_session):
        self.db = db_session

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _blob_id_from_uri(uri: str) -> str:
        """Extract the blob row id from ``inline://<id>``."""
        return uri[len("inline://"):]  # raises on malformed — callers guard

    def _row(self, uri: str):
        from app.models.artifact import ArtifactBlob

        try:
            blob_id = self._blob_id_from_uri(uri)
        except Exception:
            return None
        return self.db.query(ArtifactBlob).filter(
            ArtifactBlob.id == blob_id, ArtifactBlob.is_deleted == False
        ).first()

    # ── interface ────────────────────────────────────────────────────

    def put(self, uri: str, data: bytes) -> None:
        row = self._row(uri)
        if row is None:
            raise ValueError(f"No blob row found for uri {uri!r}")
        row.data = data
        row.file_size = len(data)
        row.checksum = hashlib.sha256(data).hexdigest()
        self.db.commit()

    def get(self, uri: str) -> Optional[bytes]:
        row = self._row(uri)
        if row is None:
            return None
        return row.data

    def delete(self, uri: str) -> bool:
        row = self._row(uri)
        if row is None or row.data is None:
            return False
        row.data = None
        row.file_size = 0
        self.db.commit()
        return True

    def exists(self, uri: str) -> bool:
        row = self._row(uri)
        return row is not None and row.data is not None


# ── MinioBlobStorage ────────────────────────────────────────────────────────


class MinioBlobStorage(BlobStorage):
    """Store blobs in MinIO (S3-compatible object storage).

    URIs use the ``s3://<bucket>/<object_key>`` scheme.  Requires the
    ``minio`` Python package and the following env vars / config keys:

    * ``MINIO_ENDPOINT`` — host:port of the MinIO server
    * ``MINIO_ACCESS_KEY`` — access key
    * ``MINIO_SECRET_KEY`` — secret key
    * ``MINIO_BUCKET`` — bucket name (default: ``zhanlu-artifacts``)
    """

    SCHEME = "s3"

    def __init__(self):
        self._client = None
        self._bucket = settings.MINIO_BUCKET or "zhanlu-artifacts"

    @property
    def client(self):
        """Lazy-init the MinIO client so imports don't fail when minio is absent."""
        if self._client is None:
            from minio import Minio

            endpoint = settings.MINIO_ENDPOINT
            if not endpoint:
                raise RuntimeError("MINIO_ENDPOINT is not configured")

            self._client = Minio(
                endpoint=endpoint,
                access_key=settings.MINIO_ACCESS_KEY,
                secret_key=settings.MINIO_SECRET_KEY,
                secure=False,  # local dev; set True for TLS in production
            )
            # Ensure the bucket exists
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)
                logger.info("Created MinIO bucket %r", self._bucket)
        return self._client

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _object_key_from_uri(uri: str) -> str:
        """Extract the object key from ``s3://<bucket>/<key>``."""
        # s3://bucket/key → key
        return uri.split("/", 3)[-1]

    # ── interface ────────────────────────────────────────────────────

    def put(self, uri: str, data: bytes) -> None:
        key = self._object_key_from_uri(uri)
        self.client.put_object(
            bucket_name=self._bucket,
            object_name=key,
            data=io.BytesIO(data),
            length=len(data),
        )

    def get(self, uri: str) -> Optional[bytes]:
        from minio.error import S3Error

        key = self._object_key_from_uri(uri)
        try:
            response = self.client.get_object(self._bucket, key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                return None
            logger.warning("MinIO get error for %s: %s", key, exc)
            return None

    def delete(self, uri: str) -> bool:
        from minio.error import S3Error

        key = self._object_key_from_uri(uri)
        try:
            self.client.remove_object(self._bucket, key)
            return True
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                return False
            logger.warning("MinIO delete error for %s: %s", key, exc)
            return False

    def exists(self, uri: str) -> bool:
        from minio.error import S3Error

        key = self._object_key_from_uri(uri)
        try:
            self.client.stat_object(self._bucket, key)
            return True
        except S3Error:
            return False


# ── Factory ─────────────────────────────────────────────────────────────────


def get_blob_storage(db_session=None) -> BlobStorage:
    """Return the configured ``BlobStorage`` backend.

    Reads ``ARTIFACT_STORAGE_BACKEND`` from settings.  Valid values:

    * ``"postgres_bytea"`` (default) — ``PostgresBlobStorage``
    * ``"minio"`` — ``MinioBlobStorage``
    """
    backend = (settings.ARTIFACT_STORAGE_BACKEND or "postgres_bytea").strip()

    if backend == "minio":
        return MinioBlobStorage()

    # Default: Postgres inline storage
    if db_session is None:
        raise ValueError(
            "db_session is required for PostgresBlobStorage but was None"
        )
    return PostgresBlobStorage(db_session)


__all__ = [
    "BlobStorage",
    "PostgresBlobStorage",
    "MinioBlobStorage",
    "get_blob_storage",
]
