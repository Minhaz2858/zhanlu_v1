"""P4 T1 — document versioning (D5: immutable, file-level identity).

One file KB = one document (cardinality verified in the decision record
D1). ``record_document_version`` computes the document's SHA-256 from the
ACTUAL file bytes and:

- appends an immutable ``document_versions`` row when the hash is new;
- leaves history untouched when the content is unchanged (returns None);
- updates the CURRENT pointer (``kb.content_hash`` / ``content_version_id``)
  on the KnowledgeBase — the pointer is the only mutable state.

A vanished / unreadable file records NOTHING (honest failure — a missing
file must not fabricate a version).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.knowledge_base import DocumentVersion, KnowledgeBase
from app.services.document_ingestion.service import _resolve_local_path

logger = logging.getLogger(__name__)


def compute_content_hash(kb: KnowledgeBase) -> tuple[str, int] | None:
    """SHA-256 over the actual file bytes. None when the file cannot be
    read (missing, outside upload root)."""
    path = _resolve_local_path(kb.file_url or "")
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError as exc:
        logger.warning("document versioning: cannot read %s: %s",
                       kb.file_url, exc)
        return None
    return hashlib.sha256(data).hexdigest(), len(data)


def record_document_version(db: Session, kb: KnowledgeBase) -> DocumentVersion | None:
    """Append a version row for the document's current bytes if the hash is
    new; update the KB's current pointer. Returns the new row, or None when
    unchanged / unreadable. The caller's transaction owns the commit."""
    if kb.source_kind != "file":
        return None
    computed = compute_content_hash(kb)
    if computed is None:
        return None
    content_hash, content_bytes = computed

    existing = (
        db.query(DocumentVersion)
        .filter(
            DocumentVersion.org_id == kb.org_id,
            DocumentVersion.kb_id == kb.id,
            DocumentVersion.content_hash == content_hash,
        )
        .first()
    )
    if existing is not None:
        # Unchanged content — no new version. Pointers stay put.
        return None

    version = DocumentVersion(
        org_id=kb.org_id,
        kb_id=kb.id,
        content_hash=content_hash,
        content_bytes=content_bytes,
        file_url_at_ingest=kb.file_url,
        first_indexed_at=datetime.now(timezone.utc),
    )
    db.add(version)
    db.flush()  # assign version.id so the pointer can reference it
    kb.content_hash = content_hash
    kb.content_version_id = version.id
    db.flush()  # persist the pointer within the caller's transaction
    logger.info("document versioning: kb=%s new version %s (%d bytes)",
                kb.id, content_hash[:12], content_bytes)
    return version
