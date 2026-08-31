"""User memory retriever — long-term per-user memory snapshots via RAG.

Persists user-specific facts, preferences, and notes into the
``user_memory`` semantic collection, then retrieves them at query time
to inject into LLM prompts as context.

User-memory retriever adapted
to zhanlu's ``app.services.rag.knowledge_base.create_knowledge_base()``
factory and ``app.services.rag.collection_names.USER_MEMORY``.

Public API:
    UserMemoryEntry (dataclass)         # single memory item
    retrieve_user_memory(user_id, query, top_k=5)
        -> List[UserMemoryEntry]
    build_user_context(user_id, query, max_chars=900)
        -> str  # formatted prompt-injection block
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Any, List

from app.services.rag.collection_names import USER_MEMORY

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------


#: Categorical memory types. New types may be added; these are the canonical ones.
KNOWN_MEMORY_TYPES: List[str] = [
    "fact",          # A factual note about the user (e.g. "user is on margin team")
    "preference",    # Stated or learned preference (e.g. "user prefers short answers")
    "context",       # Conversational context for ongoing threads
    "task",          # Active task / todo the user is tracking
    "decision",      # A past decision the user (or agent) committed to
]


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class UserMemoryEntry:
    """A single retrieved user-memory item."""

    text: str
    memory_type: str  # one of KNOWN_MEMORY_TYPES
    user_id: str
    topic: str = ""
    score: float = 0.0
    extra_metadata: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.extra_metadata is None:
            self.extra_metadata = {}

    def formatted(self, max_chars: int = 400) -> str:
        excerpt = (self.text or "")[:max_chars].replace("\n", " ").strip()
        if len(self.text or "") > max_chars:
            excerpt += "…"
        return f"[{self.memory_type}:{self.topic}] {excerpt}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_kb() -> Any:
    try:
        from app.services.rag.knowledge_base import create_knowledge_base
    except Exception as exc:  # noqa: BLE001
        logger.debug("_get_kb: import failed: %s", exc)
        return None
    try:
        org_id = os.environ.get("DEFAULT_ORG_ID", "default")
        return create_knowledge_base(org_id=org_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("_get_kb: create_knowledge_base failed: %s", exc)
        return None


def _score(distance: float) -> float:
    """Convert ChromaDB distance to a 0-1 relevance score (higher = more relevant)."""
    try:
        return math.exp(-float(distance))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def retrieve_user_memory(
    user_id: str,
    query: str,
    top_k: int = 5,
    memory_type: str = "",
) -> List[UserMemoryEntry]:
    """Retrieve user-memory entries relevant to ``query``.

    Args:
        user_id: user identifier (used as a metadata filter).
        query: natural-language query string.
        top_k: max number of entries to return.
        memory_type: optional filter — one of ``KNOWN_MEMORY_TYPES``.

    Returns:
        List of ``UserMemoryEntry`` sorted by descending relevance.
    """
    if not query or not query.strip():
        return []

    kb = _get_kb()
    if kb is None:
        return []

    coll = None
    if hasattr(kb, "get_collection"):
        try:
            coll = kb.get_collection(USER_MEMORY)
        except Exception:  # noqa: BLE001
            coll = None
    if coll is None:
        return []

    # Build where filter — always include user_id
    where: dict = {"user_id": user_id}
    if memory_type:
        where["memory_type"] = memory_type

    try:
        result = coll.query(
            query_texts=[query],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("retrieve_user_memory: coll.query failed: %s", exc)
        return []

    docs = (result.get("documents") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    dists = (result.get("distances") or [[]])[0]

    entries: List[UserMemoryEntry] = []
    for doc, meta, dist in zip(docs, metas, dists):
        if not doc:
            continue
        meta = meta or {}
        entries.append(UserMemoryEntry(
            text=doc,
            memory_type=str(meta.get("memory_type", "fact")),
            user_id=str(meta.get("user_id", user_id)),
            topic=str(meta.get("topic", "")),
            score=_score(dist),
            extra_metadata=dict(meta),
        ))

    entries.sort(key=lambda e: e.score, reverse=True)
    return entries


def build_user_context(
    user_id: str,
    query: str,
    max_chars: int = 900,
    top_k: int = 5,
) -> str:
    """Build a prompt-injection block of relevant user-memory entries.

    Returns empty string if no relevant entries found.
    """
    if not query or not query.strip():
        return ""

    entries = retrieve_user_memory(user_id, query, top_k=top_k)
    if not entries:
        return ""

    header = "[User Memory - Relevant Past Context]"
    lines: List[str] = []
    total_chars = 0
    for entry in entries:
        formatted = entry.formatted(max_chars=200)
        if total_chars + len(formatted) + 5 > max_chars:
            break
        lines.append(formatted)
        total_chars += len(formatted) + 5

    if not lines:
        return ""

    return header + "\n" + "\n".join(lines)