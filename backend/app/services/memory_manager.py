"""Memory manager -- semantic dedup, consolidation, and lifecycle management.

Extends the existing ``memory_advanced`` module with capabilities that
prevent memory bloat over time:

1. **Semantic dedup**: detect near-duplicate memories with different wording
   using embedding cosine similarity (exact-hash dedup already exists).
2. **Consolidation**: merge semantic duplicates, keeping the highest-importance
   or most-recent copy and incrementing its usage_count.
3. **Lifecycle**: archive low-importance, low-usage, expired memories;
   promote frequently-accessed memories.

Designed to run as a background task (spawned by the background review or
a scheduled job), not inline in the turn loop.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.agent_memory import AgentMemory
from app.services.memory_advanced import (
    compute_content_hash,
    _cosine_similarity,
    _compute_embedding,
    is_expired,
    filter_expired,
)

logger = logging.getLogger(__name__)

# Cosine similarity threshold above which two memories are considered
# semantic duplicates. 0.85 = very similar (near-paraphrase).
SEMANTIC_DUP_THRESHOLD = 0.85

# Memories with importance <= this AND usage_count == 0 AND older than
# STALE_DAYS are candidates for archival.
LOW_IMPORTANCE_THRESHOLD = 1
STALE_DAYS = 30

# Minimum usage_count to trigger importance promotion.
PROMOTION_USAGE_THRESHOLD = 3


@dataclass
class ConsolidationReport:
    """Summary of a consolidation run."""
    semantic_duplicates_found: int = 0
    semantic_duplicates_merged: int = 0
    expired_removed: int = 0
    stale_archived: int = 0
    promoted: int = 0
    total_before: int = 0
    total_after: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "semantic_duplicates_found": self.semantic_duplicates_found,
            "semantic_duplicates_merged": self.semantic_duplicates_merged,
            "expired_removed": self.expired_removed,
            "stale_archived": self.stale_archived,
            "promoted": self.promoted,
            "total_before": self.total_before,
            "total_after": self.total_after,
        }


def find_semantic_duplicates(
    db: Session,
    agent_app_id: str,
    threshold: float = SEMANTIC_DUP_THRESHOLD,
) -> list[tuple[AgentMemory, AgentMemory, float]]:
    """Find near-duplicate memory pairs by embedding cosine similarity.

    Returns a list of ``(memory_a, memory_b, similarity)`` tuples where
    similarity >= threshold. Only considers memories that have embeddings.
    """
    memories = db.query(AgentMemory).filter(
        AgentMemory.agent_app_id == agent_app_id,
        AgentMemory.is_deleted == False,
    ).all()

    # Only consider memories with embeddings
    with_embeddings = [m for m in memories if getattr(m, "embedding", None)]
    if len(with_embeddings) < 2:
        return []

    pairs: list[tuple[AgentMemory, AgentMemory, float]] = []
    for i in range(len(with_embeddings)):
        for j in range(i + 1, len(with_embeddings)):
            sim = _cosine_similarity(
                with_embeddings[i].embedding,
                with_embeddings[j].embedding,
            )
            if sim >= threshold:
                pairs.append((with_embeddings[i], with_embeddings[j], sim))

    return pairs


def _pick_keeper(a: AgentMemory, b: AgentMemory) -> tuple[AgentMemory, AgentMemory]:
    """Decide which memory to keep (keeper) and which to merge (loser).

    Keeper is the one with higher importance, or if equal, higher usage_count,
    or if equal, more recent.
    """
    imp_a = getattr(a, "importance", 0) or 0
    imp_b = getattr(b, "importance", 0) or 0
    if imp_a != imp_b:
        return (a, b) if imp_a > imp_b else (b, a)

    usage_a = getattr(a, "usage_count", 0) or 0
    usage_b = getattr(b, "usage_count", 0) or 0
    if usage_a != usage_b:
        return (a, b) if usage_a > usage_b else (b, a)

    # Same importance + usage — keep the newer one
    return (a, b) if str(a.created_date) >= str(b.created_date) else (b, a)


def merge_semantic_duplicates(
    db: Session,
    agent_app_id: str,
    threshold: float = SEMANTIC_DUP_THRESHOLD,
) -> int:
    """Merge semantic duplicate memories, keeping the best copy.

    The loser is soft-deleted; its usage_count is added to the keeper.
    Returns the number of memories merged (deleted).
    """
    pairs = find_semantic_duplicates(db, agent_app_id, threshold)
    if not pairs:
        return 0

    # Track which memories have already been merged (to avoid double-merge)
    merged_ids: set[str] = set()
    merged_count = 0

    for a, b, _sim in pairs:
        if a.id in merged_ids or b.id in merged_ids:
            continue  # one of them was already merged in this pass

        keeper, loser = _pick_keeper(a, b)

        # Transfer usage count
        keeper_usage = getattr(keeper, "usage_count", 0) or 0
        loser_usage = getattr(loser, "usage_count", 0) or 0
        keeper.usage_count = keeper_usage + loser_usage + 1  # +1 for the merge itself

        # Soft-delete the loser
        loser.is_deleted = True
        merged_ids.add(loser.id)
        merged_count += 1

        logger.debug(
            "Merged memory %s into %s (similarity >= %.2f)",
            loser.id, keeper.id, threshold,
        )

    db.commit()
    return merged_count


def remove_expired(db: Session, agent_app_id: str) -> int:
    """Soft-delete expired memories. Returns the count removed."""
    memories = db.query(AgentMemory).filter(
        AgentMemory.agent_app_id == agent_app_id,
        AgentMemory.is_deleted == False,
    ).all()

    removed = 0
    for m in memories:
        if is_expired(m):
            m.is_deleted = True
            removed += 1

    if removed:
        db.commit()
    return removed


def archive_stale(
    db: Session,
    agent_app_id: str,
    stale_days: int = STALE_DAYS,
    low_importance: int = LOW_IMPORTANCE_THRESHOLD,
) -> int:
    """Archive low-importance, low-usage, stale memories.

    A memory is stale if:
    - importance <= low_importance
    - usage_count == 0
    - older than stale_days

    Returns the count archived.
    """
    memories = db.query(AgentMemory).filter(
        AgentMemory.agent_app_id == agent_app_id,
        AgentMemory.is_deleted == False,
    ).all()

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=stale_days)
    archived = 0

    for m in memories:
        imp = getattr(m, "importance", 0) or 0
        usage = getattr(m, "usage_count", 0) or 0
        if imp > low_importance or usage > 0:
            continue

        created = m.created_date
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created)
            except ValueError:
                continue
        if created is not None and getattr(created, "tzinfo", None) is not None:
            # Defensive: normalize aware inputs to naive UTC like Postgres.
            created = created.astimezone(timezone.utc).replace(tzinfo=None)
        if not created or created >= cutoff:
            continue

        m.is_deleted = True
        archived += 1

    if archived:
        db.commit()
    return archived


def promote_frequently_used(
    db: Session,
    agent_app_id: str,
    promotion_threshold: int = PROMOTION_USAGE_THRESHOLD,
) -> int:
    """Promote frequently-used memories to higher importance.

    Memories with usage_count >= promotion_threshold get importance
    bumped to at least 3 (if lower). Returns the count promoted.
    """
    memories = db.query(AgentMemory).filter(
        AgentMemory.agent_app_id == agent_app_id,
        AgentMemory.is_deleted == False,
    ).all()

    promoted = 0
    for m in memories:
        usage = getattr(m, "usage_count", 0) or 0
        imp = getattr(m, "importance", 0) or 0
        if usage >= promotion_threshold and imp < 3:
            m.importance = 3
            promoted += 1

    if promoted:
        db.commit()
    return promoted


def run_consolidation(
    db: Session,
    agent_app_id: str,
    *,
    semantic_threshold: float = SEMANTIC_DUP_THRESHOLD,
    stale_days: int = STALE_DAYS,
) -> ConsolidationReport:
    """Run the full memory consolidation pipeline.

    Steps (in order):
    1. Remove expired memories.
    2. Merge semantic duplicates.
    3. Archive stale, low-importance, unused memories.
    4. Promote frequently-used memories.

    Returns a ConsolidationReport with counts.
    """
    report = ConsolidationReport()

    # Count before
    report.total_before = db.query(AgentMemory).filter(
        AgentMemory.agent_app_id == agent_app_id,
        AgentMemory.is_deleted == False,
    ).count()

    # Step 1: remove expired
    report.expired_removed = remove_expired(db, agent_app_id)

    # Step 2: merge semantic duplicates
    dups = find_semantic_duplicates(db, agent_app_id, semantic_threshold)
    report.semantic_duplicates_found = len(dups)
    report.semantic_duplicates_merged = merge_semantic_duplicates(
        db, agent_app_id, semantic_threshold
    )

    # Step 3: archive stale
    report.stale_archived = archive_stale(db, agent_app_id, stale_days)

    # Step 4: promote frequently used
    report.promoted = promote_frequently_used(db, agent_app_id)

    # Count after
    report.total_after = db.query(AgentMemory).filter(
        AgentMemory.agent_app_id == agent_app_id,
        AgentMemory.is_deleted == False,
    ).count()

    logger.info(
        "Memory consolidation for agent %s: %d -> %d (merged=%d, expired=%d, archived=%d, promoted=%d)",
        agent_app_id, report.total_before, report.total_after,
        report.semantic_duplicates_merged, report.expired_removed,
        report.stale_archived, report.promoted,
    )

    return report


__all__ = [
    "ConsolidationReport",
    "find_semantic_duplicates",
    "merge_semantic_duplicates",
    "remove_expired",
    "archive_stale",
    "promote_frequently_used",
    "run_consolidation",
    "SEMANTIC_DUP_THRESHOLD",
]
