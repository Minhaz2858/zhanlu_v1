"""Semantic response cache (experience layer, Phase B).

Layer 2: cache final assistant responses keyed by question embedding +
data version. A hit requires: same agent, matching scope, the same
``data_version``, cosine similarity >= threshold, feedback score above
the eviction floor, and a non-expired entry. **Prefer a cache miss over
serving stale market prices.**

All functions are best-effort: lookup failures fall back to a cache miss
(safe), and write failures are logged and skipped — the chat loop is
never affected. If the embedding service is unavailable the cache is
simply disabled for that turn.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.models.response_cache_entry import (
    CACHE_SCOPE_SHARED,
    CACHE_SCOPE_USER,
    DEFAULT_CACHE_SIM_THRESHOLD,
    DEFAULT_CACHE_TTL_HOURS,
    FEEDBACK_EVICTION_FLOOR,
    ResponseCacheEntry,
)

logger = logging.getLogger(__name__)

_CACHE_TTL_HOURS = DEFAULT_CACHE_TTL_HOURS
_SIM_THRESHOLD = DEFAULT_CACHE_SIM_THRESHOLD
_PRUNE_BATCH = 200

# Intents treated as conversational (per-user cache scope)
_CONVERSATIONAL_INTENTS = ("conversational", "general")


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two embedding vectors (0.0 on mismatch)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def get_market_data_version() -> str:
    """Latest trading date (YYYY-MM-DD) from the market data source.

    Queries ``MAX(FDATE)`` from ``md_t_lz_price`` via the shared external
    MySQL engine; falls back to today's UTC date on any failure.
    Best-effort — never raises.
    """
    try:
        from app.core.mysql_db import get_mysql_engine

        eng = get_mysql_engine()
        if eng is not None:
            with eng.connect() as conn:
                row = conn.execute(sql_text("SELECT MAX(FDATE) FROM md_t_lz_price")).fetchone()
                if row and row[0] is not None:
                    val = str(row[0])[:10]
                    if len(val) == 10 and val.count("-") == 2:
                        return val
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug("market data version lookup failed (non-fatal): %s", exc)
    return datetime.now(timezone.utc).date().isoformat()


def lookup_cached_response(
    db: Session,
    *,
    agent_app_id: str,
    user_id: Optional[str],
    question_text: str,
    intent_class: str,
    embedding: Optional[list[float]],
    data_version: Optional[str] = None,
) -> Optional[ResponseCacheEntry]:
    """Return the best matching fresh cache entry, or ``None``.

    Strict guards: same agent + matching scope (shared for data-driven
    intents, per-user for conversational) + same ``data_version`` +
    similarity >= threshold + feedback above floor + not expired.
    ``embedding`` unavailable => ``None`` (cache disabled this turn).
    """
    if not embedding:
        return None
    try:
        prune_expired(db)
        if data_version is None:
            data_version = get_market_data_version()
        q = (
            db.query(ResponseCacheEntry)
            .filter(
                ResponseCacheEntry.agent_app_id == agent_app_id,
                ResponseCacheEntry.data_version == data_version,
                ResponseCacheEntry.expires_at > datetime.now(timezone.utc),
                ResponseCacheEntry.is_deleted == False,  # noqa: E712
            )
        )
        if intent_class in _CONVERSATIONAL_INTENTS:
            q = q.filter(
                ResponseCacheEntry.scope == CACHE_SCOPE_USER,
                ResponseCacheEntry.user_id == user_id,
            )
        else:
            q = q.filter(ResponseCacheEntry.scope == CACHE_SCOPE_SHARED)
        entries = q.all()
        best = None
        best_sim = 0.0
        for e in entries:
            if e.feedback_score <= FEEDBACK_EVICTION_FLOOR:
                continue
            sim = _cosine_similarity(embedding, e.question_embedding or [])
            if sim >= _SIM_THRESHOLD and sim > best_sim:
                best, best_sim = e, sim
        if best is not None:
            best.hit_count += 1
            db.commit()
            logger.debug(
                "response cache HIT agent=%s intent=%s sim=%.3f dv=%s",
                agent_app_id, intent_class, best_sim, data_version,
            )
        return best
    except Exception as exc:  # noqa: BLE001 — safe fallback
        logger.warning("response cache lookup failed (non-fatal): %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def store_cached_response(
    db: Session,
    *,
    agent_app_id: str,
    user_id: Optional[str],
    question_text: str,
    intent_class: str,
    embedding: Optional[list[float]],
    response_content: str,
    artifact_ids: Optional[list] = None,
    data_version: Optional[str] = None,
) -> Optional[ResponseCacheEntry]:
    """Persist a successful turn's response for future semantic hits.

    Data-driven intents are stored with shared scope; conversational
    intents with per-user scope (requires a user). Best-effort: any
    failure is logged and skipped.
    """
    if not response_content or not response_content.strip():
        return None
    if not embedding:
        return None  # no embedding -> cannot be matched later
    try:
        if data_version is None:
            data_version = get_market_data_version()
        scope = CACHE_SCOPE_SHARED
        if intent_class in _CONVERSATIONAL_INTENTS:
            if not user_id:
                return None  # nothing to key a per-user cache on
            scope = CACHE_SCOPE_USER
        entry = ResponseCacheEntry(
            agent_app_id=agent_app_id,
            scope=scope,
            user_id=user_id if scope == CACHE_SCOPE_USER else None,
            intent_class=intent_class,
            question_text=question_text[:2000],
            question_embedding=embedding,
            response_content=response_content,
            artifact_ids=artifact_ids or [],
            data_version=data_version,
            feedback_score=0.0,
            hit_count=0,
            expires_at=ResponseCacheEntry.ttl_from_now(_CACHE_TTL_HOURS),
        )
        db.add(entry)
        db.commit()
        logger.debug(
            "response cache STORE agent=%s intent=%s scope=%s dv=%s",
            agent_app_id, intent_class, scope, data_version,
        )
        return entry
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("response cache store failed (non-fatal): %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def evict_cache_entry(db: Session, entry_id: str) -> bool:
    """Soft-delete a cache entry (used on thumbs-down feedback)."""
    try:
        e = (
            db.query(ResponseCacheEntry)
            .filter(ResponseCacheEntry.id == entry_id, ResponseCacheEntry.is_deleted == False)  # noqa: E712
            .first()
        )
        if e:
            e.is_deleted = True
            db.commit()
            return True
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("response cache evict failed (non-fatal): %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    return False


def apply_feedback_score(db: Session, entry_id: str, rating: int) -> None:
    """Adjust a cache entry's feedback score (+1 / -1) and evict below floor."""
    try:
        e = (
            db.query(ResponseCacheEntry)
            .filter(ResponseCacheEntry.id == entry_id, ResponseCacheEntry.is_deleted == False)  # noqa: E712
            .first()
        )
        if not e:
            return
        e.feedback_score = float(e.feedback_score or 0.0) + (1.0 if rating > 0 else -1.0)
        db.commit()
        if e.feedback_score <= FEEDBACK_EVICTION_FLOOR:
            evict_cache_entry(db, e.id)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("response cache feedback adjust failed (non-fatal): %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def prune_expired(db: Session) -> int:
    """Soft-delete expired entries (bounded batch). Returns count removed."""
    try:
        rows = (
            db.query(ResponseCacheEntry)
            .filter(ResponseCacheEntry.expires_at <= datetime.now(timezone.utc))
            .limit(_PRUNE_BATCH)
            .all()
        )
        for r in rows:
            r.is_deleted = True
        if rows:
            db.commit()
        return len(rows)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug("response cache prune failed (non-fatal): %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0
