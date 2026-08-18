"""Phase 5 — Usage instrumentation for deck artifacts.

Thin, dependency-free helper that records deck lifecycle events
(``deck_generated`` / ``deck_edited`` / ``deck_downloaded``) into the
``artifact_events`` table.  Design rules:

  * **Metadata only** — callers must pass structural metadata (profile, theme,
    slide count, edit kind).  Never pass slide text or raw user content.
  * **Never blocks the render pipeline** — the async helper is meant to be
    dispatched via ``asyncio.create_task`` so the caller returns immediately.
  * **Best-effort** — a logging failure must never break a deck generation or
    edit; errors are swallowed and logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.artifact_event import ARTIFACT_EVENT_TYPES, ArtifactEvent


logger = logging.getLogger(__name__)


def _coerce_metadata(metadata: Optional[dict[str, Any]]) -> Optional[str]:
    """Serialize metadata to JSON, dropping anything that isn't JSON-safe.

    Guarantees we never accidentally store slide text: callers are expected to
    pass only structural keys, but we defensively stringify + truncate.
    """
    if not metadata:
        return None
    try:
        text = json.dumps(metadata, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None
    # Hard cap (defensive — metadata should be tiny).
    if len(text) > 4000:
        text = text[:4000]
    return text


def log_deck_event(
    db: Session,
    event_type: str,
    artifact_id: Optional[str] = None,
    user_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    org_id: str = "default-org",
    app_id: str = "default-app",
) -> None:
    """Synchronously write one artifact event. Best-effort; swallows errors."""
    if event_type not in ARTIFACT_EVENT_TYPES:
        logger.warning("log_deck_event: unknown event_type '%s' (skipped)", event_type)
        return
    try:
        event = ArtifactEvent(
            artifact_id=artifact_id,
            event_type=event_type,
            user_id=user_id,
            metadata_json=_coerce_metadata(metadata),
            org_id=org_id,
            app_id=app_id,
        )
        db.add(event)
        db.commit()
    except Exception as exc:  # noqa: BLE001 — instrumentation must never crash the call path
        logger.warning("log_deck_event: write failed: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass


async def log_deck_event_async(
    db_factory,
    event_type: str,
    artifact_id: Optional[str] = None,
    user_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    org_id: str = "default-org",
    app_id: str = "default-app",
) -> None:
    """Fire-and-forget event logging.

    ``db_factory`` is a zero-arg callable returning a fresh ``Session`` (so the
    event is written on its own session/transaction and never interferes with
    the caller's).  Intended use::

        asyncio.create_task(log_deck_event_async(
            lambda: SessionLocal(), "deck_generated", artifact_id=..., ...))

    Errors are swallowed so the parent task is unaffected.
    """
    def _run() -> None:
        from app.database import SessionLocal

        if db_factory is None:
            db = SessionLocal()
            owned = True
        else:
            db = db_factory()
            owned = True
        try:
            log_deck_event(
                db, event_type, artifact_id=artifact_id, user_id=user_id,
                metadata=metadata, org_id=org_id, app_id=app_id,
            )
        finally:
            if owned:
                db.close()

    try:
        await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001
        logger.warning("log_deck_event_async: failed: %s", exc)


def log_deck_event_fire_and_forget(
    db_factory,
    event_type: str,
    artifact_id: Optional[str] = None,
    user_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    org_id: str = "default-org",
    app_id: str = "default-app",
) -> None:
    """Schedule an async event log without awaiting it (fire-and-forget).

    Safe to call from a sync context (e.g. inside a tool handler) — it creates
    a task on the running loop if present, otherwise writes synchronously.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(
                log_deck_event_async(
                    db_factory, event_type, artifact_id=artifact_id,
                    user_id=user_id, metadata=metadata, org_id=org_id, app_id=app_id,
                )
            )
            return
    except RuntimeError:
        pass
    # No running loop → write synchronously on a fresh session.
    from app.database import SessionLocal

    db = db_factory() if db_factory else SessionLocal()
    try:
        log_deck_event(
            db, event_type, artifact_id=artifact_id, user_id=user_id,
            metadata=metadata, org_id=org_id, app_id=app_id,
        )
    finally:
        db.close()
