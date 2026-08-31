"""Advanced memory system — adapted from OpenHarness.

Extends Zhanlu's AgentMemory with:
- SHA-256 content signature for deduplication
- TTL (time-to-live) for automatic expiration
- Importance scoring and usage tracking
- Relevance-based search (keyword matching + scoring)
- Auto-extract: LLM-based memory extraction after conversations
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.agent_memory import AgentMemory

logger = logging.getLogger(__name__)


def compute_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def is_duplicate_content(db: Session, agent_app_id: str, content: str) -> bool:
    content_hash = compute_content_hash(content)
    existing = db.query(AgentMemory).filter(
        AgentMemory.agent_app_id == agent_app_id,
        AgentMemory.content_hash == content_hash,
        AgentMemory.is_deleted == False,
    ).first()
    return existing is not None


def is_expired(memory: AgentMemory) -> bool:
    if not hasattr(memory, "ttl_days") or not memory.ttl_days:
        return False
    created = memory.created_date
    if not created:
        return False
    if isinstance(created, str):
        try:
            created = datetime.fromisoformat(created)
        except ValueError:
            return False
    if getattr(created, "tzinfo", None) is not None:
        # Defensive: normalize aware inputs to naive UTC like Postgres.
        created = created.astimezone(timezone.utc).replace(tzinfo=None)
    expiry = created + timedelta(days=memory.ttl_days)
    # Postgres TIMESTAMP columns are tz-naive UTC; build a naive now so the
    # comparison never raises "can't compare offset-naive and offset-aware".
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now > expiry


def filter_expired(memories: list[AgentMemory]) -> list[AgentMemory]:
    return [m for m in memories if not is_expired(m)]


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    words = re.findall(r"\w+", text.lower())
    for word in words:
        has_han = any("\u4e00" <= ch <= "\u9fff" for ch in word)
        if has_han:
            for ch in word:
                if "\u4e00" <= ch <= "\u9fff":
                    tokens.append(ch)
                elif ch.isalnum():
                    tokens.append(ch)
        else:
            tokens.append(word)
    return tokens


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-ish-length float vectors.

    Pads the shorter vector with zeros if lengths differ (defensive against
    providers that change embedding dimensionality). Returns 0.0 for empty
    or zero-norm vectors.
    """
    if not a or not b:
        return 0.0
    n = max(len(a), len(b))
    if len(a) < n:
        a = a + [0.0] * (n - len(a))
    elif len(b) < n:
        b = b + [0.0] * (n - len(b))
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _recency_score(memory: AgentMemory, now: datetime) -> float:
    """Recency in [0, 1] — decays linearly to 0 over 30 days."""
    created = memory.created_date
    if isinstance(created, str):
        try:
            created = datetime.fromisoformat(created)
        except ValueError:
            return 0.0
    if not created:
        return 0.0
    age_days = (now - created).days
    if age_days < 0:
        return 1.0
    return max(0.0, 1.0 - age_days / 30.0)


def _importance_score(memory: AgentMemory) -> float:
    """Importance normalized to [0, 1] assuming a 1–5 scale."""
    importance = getattr(memory, "importance", 0) or 0
    try:
        return max(0.0, min(1.0, float(importance) / 5.0))
    except (TypeError, ValueError):
        return 0.0


def _get_query_embedding(query: str) -> list[float] | None:
    """Best-effort embedding for a search query.

    Returns None (→ lexical fallback) when embeddings are disabled, the
    provider lacks an /embeddings endpoint, or the call fails.
    """
    try:
        from app.services.llm_service import get_embedding
        return get_embedding(query)
    except Exception as e:
        logger.debug("query embedding unavailable, using lexical search: %s", e)
        return None


@dataclass
class SearchResult:
    memory: AgentMemory
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {"memory": self.memory.to_dict(), "score": round(self.score, 4)}


def search_memories(
    db: Session,
    agent_app_id: str,
    query: str,
    limit: int = 10,
    min_score: float = 0.1,
) -> list[SearchResult]:
    """Search agent memories, preferring semantic recall with a lexical fallback.

    Scoring strategy:
      - When a query embedding is available AND a memory has a stored
        embedding, score by a blend of cosine similarity (0.6), recency
        (0.25) and importance (0.15). This is the semantic path and the
        primary differentiator vs. pure keyword recall.
      - Otherwise fall back to lexical token-overlap scoring (the previous
        behaviour), so recall still works in dev/offline or with providers
        that lack an /embeddings endpoint.

    Memories are always scoped by ``agent_app_id`` (the previous FSM context
    assembler ignored scope entirely — see context_assembler fix).
    """
    query_tokens = set(_tokenize(query))

    memories = db.query(AgentMemory).filter(
        AgentMemory.agent_app_id == agent_app_id,
        AgentMemory.is_deleted == False,
    ).all()
    memories = filter_expired(memories)
    if not memories:
        return []

    now = datetime.now(timezone.utc)
    query_vec = _get_query_embedding(query)
    semantic = query_vec is not None

    results: list[SearchResult] = []
    for memory in memories:
        content = memory.content or ""
        mem_vec = getattr(memory, "embedding", None)

        if semantic and query_vec and mem_vec:
            cosine = _cosine_similarity(query_vec, mem_vec)
            # Blend: semantic similarity dominates; recency/importance break
            # ties between semantically-equivalent memories.
            total_score = (
                0.60 * cosine
                + 0.25 * _recency_score(memory, now)
                + 0.15 * _importance_score(memory)
            )
        else:
            # Lexical fallback path.
            content_tokens = set(_tokenize(content))
            overlap = query_tokens & content_tokens
            if not overlap:
                continue
            base_score = len(overlap) / len(query_tokens) if query_tokens else 0.0
            usage_count = getattr(memory, "usage_count", 0) or 0
            usage_bonus = min(0.1, usage_count * 0.01)
            total_score = (
                base_score
                + usage_bonus
                + 0.2 * _importance_score(memory)
                + 0.1 * _recency_score(memory, now)
            )

        if total_score >= min_score:
            results.append(SearchResult(memory=memory, score=total_score))

    results.sort(key=lambda r: -r.score)
    return results[:limit]


def _compute_embedding(content: str) -> list[float] | None:
    """Best-effort embedding for memory content (swallows all errors)."""
    try:
        from app.services.llm_service import get_embedding
        return get_embedding(content)
    except Exception as e:
        logger.debug("memory embedding skipped: %s", e)
        return None


def save_memory(
    db: Session,
    agent_app_id: str,
    content: str,
    target: str = "memory",
    user_id: str | None = None,
    importance: int = 0,
    ttl_days: int | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Persist a memory, deduped by content hash.

    Project scoping (2026-08-27): when ``project_id`` is provided, the
    dedup lookup matches rows with that exact project_id and the new row
    is created with that project_id — so auto-extracted memories attach
    to the project the conversation belongs to and show up in that
    project's Shared Memory panel. ``target='user'`` rows are always
    cross-project (they describe WHO the user is), so project_id is
    forced to None for them, mirroring memory_tool.py.
    """
    if target == "user":
        project_id = None  # user profile is always cross-project

    content_hash = compute_content_hash(content)
    existing_query = db.query(AgentMemory).filter(
        AgentMemory.agent_app_id == agent_app_id,
        AgentMemory.content_hash == content_hash,
        AgentMemory.is_deleted == False,  # noqa: E712
    )
    # Strict project match (no NULL fallback) so a project-scoped write
    # never dedupes against — or attaches to — the legacy NULL bucket.
    if project_id:
        existing_query = existing_query.filter(AgentMemory.project_id == project_id)
    else:
        existing_query = existing_query.filter(AgentMemory.project_id.is_(None))
    existing = existing_query.first()

    if existing:
        if hasattr(existing, "usage_count"):
            existing.usage_count = (existing.usage_count or 0) + 1
        # Lazy heal: backfill an embedding on a duplicate touch if the
        # record predates the embedding column (best-effort, non-fatal).
        if getattr(existing, "embedding", None) is None:
            existing.embedding = _compute_embedding(existing.content or content)
        db.commit()
        return {"success": True, "duplicate": True, "id": existing.id, "message": "Duplicate memory"}

    memory = AgentMemory(
        agent_app_id=agent_app_id,
        user_id=user_id,
        project_id=project_id,
        target=target,
        content=content,
        char_count=len(content),
        content_hash=content_hash,
        importance=importance,
        ttl_days=ttl_days,
        usage_count=0,
        embedding=_compute_embedding(content),
    )
    db.add(memory)
    db.commit()
    db.refresh(memory)
    return {"success": True, "duplicate": False, "id": memory.id, "message": "Memory saved"}


def get_memories(
    db: Session,
    agent_app_id: str,
    target: str | None = None,
    limit: int = 50,
) -> list[AgentMemory]:
    query = db.query(AgentMemory).filter(
        AgentMemory.agent_app_id == agent_app_id,
        AgentMemory.is_deleted == False,
    )
    if target:
        query = query.filter(AgentMemory.target == target)
    memories = query.order_by(AgentMemory.created_date.desc()).limit(limit).all()
    return filter_expired(memories)


def delete_memory(db: Session, memory_id: str) -> bool:
    memory = db.query(AgentMemory).filter(
        AgentMemory.id == memory_id,
        AgentMemory.is_deleted == False,
    ).first()
    if not memory:
        return False
    memory.is_deleted = True
    db.commit()
    return True


EXTRACT_PROMPT = """Analyze the following conversation and extract important information worth remembering.
Respond as a JSON array of objects with "content", "target" ("memory" or "user"), and "importance" (1-5).
Only extract genuinely useful information. Skip trivial details.

Conversation:
"""


async def auto_extract_memories(
    db: Session,
    agent_app_id: str,
    messages: list[dict[str, Any]],
    user_id: str | None = None,
    max_memories: int = 5,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Extract memories from a conversation using LLM.

    ``project_id`` (2026-08-27): when the conversation is bound to a
    project, extracted memories are saved with that project_id so they
    surface in the project's Shared Memory panel.
    """
    if not messages or len(messages) < 4:
        return []

    try:
        from app.services.llm_service import call_llm

        conv_text = "\n".join(
            f"{m.get('role', 'unknown')}: {str(m.get('content', ''))[:500]}"
            for m in messages[-20:]
            if m.get("content")
        )

        result = await call_llm(
            messages=[
                {"role": "system", "content": "You are a memory extraction agent. Respond with valid JSON only."},
                {"role": "user", "content": EXTRACT_PROMPT + conv_text},
            ],
            temperature=0.3,
        )

        response = result.get("response", "[]")
        # Parse JSON response
        try:
            memories_data = json.loads(response)
        except json.JSONDecodeError:
            # Try to extract JSON array from text
            import re as _re
            match = _re.search(r'\[.*\]', response, _re.DOTALL)
            if match:
                memories_data = json.loads(match.group())
            else:
                memories_data = []

        if not isinstance(memories_data, list):
            memories_data = []

        saved: list[dict[str, Any]] = []
        for item in memories_data[:max_memories]:
            if not isinstance(item, dict):
                continue
            content = item.get("content", "").strip()
            if not content:
                continue
            target = item.get("target", "memory")
            importance = int(item.get("importance", 3))
            save_result = save_memory(
                db, agent_app_id, content, target=target,
                user_id=user_id, importance=importance,
                project_id=project_id,
            )
            if save_result.get("success") and not save_result.get("duplicate"):
                saved.append(save_result)

        return saved

    except Exception as e:
        logger.warning("Auto-extract memories failed: %s", e)
        return []


def backfill_embeddings(
    db: Session,
    agent_app_id: str | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    """Compute + persist embeddings for memories that don't have one yet.

    Intended to be run once after deploying the embedding column (see
    ``backend/scripts/backfill_memory_embeddings.py``) so that pre-existing
    memories become semantically retrievable. Safe to re-run: it only
    touches rows whose ``embedding`` is NULL.

    Args:
        db: SQLAlchemy session.
        agent_app_id: Scope to one agent; None = all agents.
        limit: Cap on number of rows to process (None = no cap).

    Returns:
        ``{"processed": n, "embedded": m, "failed": k}`` summary dict.
    """
    # NOTE: we filter "no embedding" at the Python level rather than with
    # ``embedding.is_(None)``. SQLAlchemy's JSON column serializes Python
    # ``None`` to the JSON literal ``"null"`` (a TEXT value) on some backends
    # (notably SQLite) instead of SQL NULL, so an ``IS NULL`` predicate
    # silently matches zero rows. Deserialization still yields Python ``None``
    # in both cases, so an in-Python ``not embedding`` check is the robust
    # selector. This is a one-shot maintenance path, not a hot loop.
    query = db.query(AgentMemory).filter(AgentMemory.is_deleted == False)
    if agent_app_id:
        query = query.filter(AgentMemory.agent_app_id == agent_app_id)
    candidates = query.all()

    pending: list[AgentMemory] = [
        m for m in candidates if not getattr(m, "embedding", None)
    ]
    if limit:
        pending = pending[:limit]

    processed = 0
    embedded = 0
    failed = 0
    for memory in pending:
        processed += 1
        vec = _compute_embedding(memory.content or "")
        if vec:
            memory.embedding = vec
            embedded += 1
        else:
            failed += 1
    if pending:
        db.commit()
    logger.info(
        "backfill_embeddings: processed=%d embedded=%d failed=%d",
        processed, embedded, failed,
    )
    return {"processed": processed, "embedded": embedded, "failed": failed}
