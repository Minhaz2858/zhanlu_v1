"""Ingestion orchestrator — turn a file-kind KB into embedded chunks.

Entry point ``ingest_kb(db, kb_id)`` is synchronous (run inside
``asyncio.to_thread`` from the HTTP layer). It updates
``KnowledgeBase.indexing_status`` through the lifecycle
``pending -> indexing -> ready | failed`` so the UI can poll.

``prepare_for_context(file_url)`` is the per-turn entry point used by
the chat loop: it resolves a ``file_url`` to a local path, runs the
appropriate extractor, and returns a structured dict the
context_assembler can fold into the LLM prompt. Image files return
``is_image=True`` so the caller can forward them as multimodal content
blocks.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.knowledge_base import KnowledgeBase
from app.services.document_ingestion import chunker, extractors, store

logger = logging.getLogger(__name__)


# Per-file character cap on extracted text. ~120k chars ≈ 30k tokens —
# chosen so a single big DOCX/PDF fits in a 128k-context model with room
# left over for the system prompt, history, and the response. Larger files
# are truncated with a marker so the agent knows there's more.
MAX_EXTRACTED_CHARS = 120_000

# Image extensions that should be passed through to the LLM as image
# content blocks (rather than OCR'd). When OCR is available it is still
# run and the text is prepended, but the raw bytes are also returned so
# the LLM can see the image natively.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif"}

# Text-based extension → file_type understood by extractors.extract_text.
_EXT_TO_TYPE = {
    ".txt": "txt",
    ".md": "md",
    ".csv": "csv",
    ".xlsx": "xlsx",
    ".xls": "xls",
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".ppt": "ppt",
    ".html": "html",
    ".htm": "html",
    ".json": "json",
}

# Audio extensions → Whisper transcription (gated by AUDIO_TRANSCRIBE_ENABLED).
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".m4b", ".aac"}

# Video extensions → marker stub (true transcription needs ffmpeg, deferred).
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}


def _upload_root() -> Path:
    """Patchable in tests. Returns the on-disk uploads directory."""
    from app.config import settings
    return settings.upload_path


def _resolve_local_path(file_url: str) -> Path | None:
    """Turn ``/api/uploads/<name>`` into an absolute filesystem path.

    Only paths that resolve UNDER the upload root are allowed. This
    prevents path traversal attacks (e.g. ``/etc/passwd``,
    ``../../.env``). Absolute paths outside the upload root are rejected.
    """
    if not file_url:
        return None
    upload_root = _upload_root().resolve()
    prefix = "/api/uploads/"
    if file_url.startswith(prefix):
        candidate = upload_root / file_url[len(prefix):]
    else:
        p = Path(file_url)
        if p.is_absolute():
            candidate = p
        else:
            candidate = upload_root / file_url
    # Resolve and verify the path is under the upload root
    try:
        resolved = candidate.resolve()
    except (ValueError, OSError):
        return None
    if resolved != upload_root and upload_root not in resolved.parents:
        return None
    return resolved


def _detect_type(file_url: str, fallback: str = "") -> str:
    """Infer the file_type from the file_url's extension.

    Used when the caller doesn't already know the type (the chat-upload
    path only stores the URL, not a parsed type). Falls back to ``fallback``
    (the caller-supplied content_type minus the ``application/`` prefix).
    """
    ext = os.path.splitext(file_url or "")[1].lower().lstrip(".")
    if ext in _EXT_TO_TYPE:
        return _EXT_TO_TYPE[ext]
    if ext in {e.lstrip(".") for e in _IMAGE_EXTS}:
        return ext
    # Content-type fallback (e.g. "application/pdf" -> "pdf")
    if fallback and "/" in fallback:
        return fallback.rsplit("/", 1)[-1].lower()
    return ext


def build_image_content_blocks(file_urls: list[str], max_images: int = 5) -> list[dict]:
    """Build OpenAI-compatible image content blocks for the LLM.

    For each image file_url in ``file_urls``, reads the file from disk,
    base64-encodes it, and returns a content block:
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}

    Non-image files and missing files are silently skipped. Returns at
    most ``max_images`` blocks (per OpenAI's per-turn image cap). Used by
    the chat loop to forward uploaded images to gpt-4o / claude-sonnet /
    similar multimodal models as native vision inputs (in addition to any
    OCR text extracted via prepare_for_context).
    """
    import base64

    blocks: list[dict] = []
    for furl in file_urls or []:
        if len(blocks) >= max_images:
            break
        if not isinstance(furl, str) or not furl:
            continue
        try:
            local = _resolve_local_path(furl)
            if local is None or not local.exists():
                continue
            ext = local.suffix.lower().lstrip(".")
            if ext not in {e.lstrip(".") for e in _IMAGE_EXTS}:
                continue
            mime = _ext_to_mime(ext)
            with open(local, "rb") as f:
                data = f.read()
            b64 = base64.b64encode(data).decode("ascii")
            blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
        except Exception as e:
            logger.warning("build_image_content_blocks: skipped %s: %s", furl, e)
    return blocks


def _ext_to_mime(ext: str) -> str:
    """Map an image extension to a MIME type for data URLs."""
    m = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "gif": "image/gif",
        "bmp": "image/bmp",
        "tiff": "image/tiff",
        "tif": "image/tiff",
    }
    return m.get(ext.lstrip(".").lower(), "image/png")


def prepare_for_context(file_url: str, max_chars: int = MAX_EXTRACTED_CHARS) -> dict:
    """Resolve ``file_url`` to a local path, extract text, and return a
    context-ready dict.

    Returns:
        {
            "file_url": str,         # the input url
            "file_name": str,        # basename of the resolved path
            "file_type": str,        # normalised type (pdf, docx, png, ...)
            "text": str,             # extracted text (may be empty for images)
            "is_image": bool,        # True for image files
            "local_path": str|None,  # absolute path on disk (None if missing)
            "truncated": bool,       # True if text was capped at max_chars
            "error": str|None,       # non-None when something went wrong
        }

    Never raises — any failure is captured in ``error`` so the chat loop
    can still emit a turn (with a "[could not read X]" marker) instead of
    dropping the message entirely.
    """
    out = {
        "file_url": file_url,
        "file_name": "",
        "file_type": "",
        "text": "",
        "is_image": False,
        "local_path": None,
        "truncated": False,
        "error": None,
    }
    try:
        local = _resolve_local_path(file_url)
        if local is None:
            out["error"] = "invalid file_url"
            return out
        out["local_path"] = str(local)
        out["file_name"] = local.name

        if not local.exists():
            out["error"] = f"file not found on disk: {local}"
            return out

        ext = local.suffix.lower()
        out["file_type"] = _detect_type(file_url)
        out["is_image"] = ext in _IMAGE_EXTS

        # Dispatch by extension: audio → Whisper, video → marker stub,
        # everything else → the text extractors.
        if ext in AUDIO_EXTS:
            text = extractors.extract_audio(str(local))
            if not text:
                # Always tell the LLM the file exists, even when Whisper is
                # disabled or transcription failed.
                text = f"[Audio attached: {local.name}]\n(Audio transcription unavailable.)"
        elif ext in VIDEO_EXTS:
            text = extractors.extract_video(str(local))
        else:
            text = extractors.extract_text(str(local), out["file_type"])
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[... truncated: file exceeds per-file context cap ...]"
            out["truncated"] = True
        out["text"] = text

        # Image files with no OCR text still need to be visible to the LLM
        # — the caller will forward them as multimodal image content
        # blocks. Mark them explicitly so the context_assembler knows.
        if out["is_image"] and not text:
            out["error"] = None  # not an error — multimodal path takes over
        return out
    except Exception as e:
        logger.exception("prepare_for_context failed for %s: %s", file_url, e)
        out["error"] = str(e)
        return out


def ingest_kb(db: Session, kb_id: str) -> bool:
    """Extract -> chunk -> embed -> upsert for one KB. Returns True on success."""
    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )
    if kb is None:
        logger.warning("ingest_kb: kb %s not found", kb_id)
        return False
    if kb.source_kind != "file":
        logger.info("ingest_kb: kb %s is source_kind=%s, skipping", kb_id, kb.source_kind)
        return False

    kb.indexing_status = "pending"
    kb.index_error = None
    db.commit()

    try:
        local = _resolve_local_path(kb.file_url or "")
        if local is None or not local.exists():
            raise FileNotFoundError(
                f"file not found on disk: {local} (url={kb.file_url})"
            )

        text = extractors.extract_text(str(local), kb.file_type or "")
        if not text.strip():
            raise ValueError("extracted text is empty")

        chunks = chunker.chunk_text(text, max_tokens=800, overlap=100)
        if not chunks:
            raise ValueError("chunker produced 0 chunks")

        kb.indexing_status = "indexing"
        db.commit()

        # wipe previous chunks for this KB (reindex path)
        store.delete_kb(org_id=kb.org_id, kb_id=kb.id)

        metas = [
            {"file_name": local.name, "file_type": kb.file_type or ""}
            for _ in chunks
        ]
        n = store.upsert_chunks(
            org_id=kb.org_id, kb_id=kb.id, chunks=chunks, metas=metas
        )

        kb.indexing_status = "ready"
        kb.chunk_count = n
        kb.index_error = None
        kb.last_indexed_at = datetime.now(timezone.utc)
        db.commit()
        logger.info("ingest_kb: kb %s ready, %d chunks", kb_id, n)

        # Unified Resource Registry (flag-gated, best-effort): register the
        # ingested document store as a `file` resource for its projects.
        try:
            from app.config import settings as _settings

            if getattr(_settings, "KG_RESOURCE_REGISTRY_ENABLED", False):
                from app.models.project import Project
                from app.services.knowledge_graph.registry_indexer import index_document

                pids = {kb.project_id} if getattr(kb, "project_id", None) else set()
                legacy = getattr(kb, "project", None)
                if legacy:
                    for p in (
                        db.query(Project)
                        .filter(Project.name == legacy, Project.is_deleted == False)  # noqa: E712
                        .all()
                    ):
                        pids.add(p.id)
                for pid in pids:
                    if pid:
                        index_document(
                            db,
                            project_id=pid,
                            document_id=kb.id,
                            name=kb.name,
                            summary=(
                                f"Document store '{kb.name}' — {n} chunks indexed."
                            ),
                            owner_user_id=getattr(kb, "created_by_id", None),
                        )
                if pids:
                    db.commit()
        except Exception:
            logger.debug("ingest_kb: registry sync failed (non-fatal)", exc_info=True)
        return True

    except Exception as e:
        logger.exception("ingest_kb failed for kb %s: %s", kb_id, e)
        kb.indexing_status = "failed"
        kb.index_error = str(e)[:500]
        db.commit()
        return False


def delete_index(db: Session, kb_id: str) -> None:
    """Drop all vectors for a KB (call on delete)."""
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
    if kb is None:
        return
    try:
        store.delete_kb(org_id=kb.org_id, kb_id=kb_id)
    except Exception as e:
        logger.warning("delete_index failed for kb %s: %s", kb_id, e)


def get_status(db: Session, kb_id: str) -> dict:
    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )
    if kb is None:
        return {"found": False}
    return {
        "found": True,
        "kb_id": kb.id,
        "indexing_status": kb.indexing_status,
        "chunk_count": kb.chunk_count,
        "index_error": kb.index_error,
        "last_indexed_at": kb.last_indexed_at.isoformat() if kb.last_indexed_at else None,
    }
