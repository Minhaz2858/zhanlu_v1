"""ProjectMemoryService — shared, project-scoped memory for all agents."""

import hashlib
import logging

from datetime import datetime, timedelta, timezone
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models.project_memory import ProjectMemory

logger = logging.getLogger(__name__)

# Character limit for the project memory snapshot injected into agent prompts
PROJECT_MEMORY_CHAR_LIMIT = 5000


class ProjectMemoryService:
    """Manage project-scoped shared memory.

    All agents operating within the same project contribute to and read
    from this memory, providing continuity across agent boundaries.
    """

    def __init__(self, db: Session):
        self.db = db

    # ── write ─────────────────────────────────────────────────────────

    def write_entry(
        self,
        project_id: str,
        content: str,
        entry_type: str = "fact",
        agent_app_id: str | None = None,
        user_id: str | None = None,
        importance: int = 0,
        ttl_days: int | None = None,
        source_conversation_id: str | None = None,
        source_artifact_id: str | None = None,
    ) -> ProjectMemory:
        """Add an entry to project memory, deduplicating by content hash."""
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # Dedup: skip if identical content already exists for this project
        existing = self.db.query(ProjectMemory).filter(
            ProjectMemory.project_id == project_id,
            ProjectMemory.content_hash == content_hash,
            ProjectMemory.is_deleted == False,
        ).first()
        if existing:
            existing.usage_count = (existing.usage_count or 0) + 1
            existing.last_accessed_at = datetime.now(timezone.utc)
            self.db.flush()
            logger.debug(
                "ProjectMemory dedup hit | project=%s hash=%s… usage=%s",
                project_id, content_hash[:8], existing.usage_count,
            )
            return existing

        entry = ProjectMemory(
            project_id=project_id,
            agent_app_id=agent_app_id,
            entry_type=entry_type,
            content=content,
            content_hash=content_hash,
            importance=importance,
            ttl_days=ttl_days,
            usage_count=0,
            source_conversation_id=source_conversation_id,
            source_artifact_id=source_artifact_id,
            created_by_id=user_id,
        )
        self.db.add(entry)
        self.db.flush()

        # Unified Resource Registry sync (best-effort, flag-gated).
        # Keeps project Knowledge Map aligned with newly written memory.
        try:
            if getattr(settings, "KG_RESOURCE_REGISTRY_ENABLED", False):
                from app.services.knowledge_graph.registry_indexer import (
                    index_decision,
                    index_memory_entry,
                )

                index_memory_entry(
                    self.db,
                    project_id=project_id,
                    memory_id=entry.id,
                    summary=content,
                    owner_user_id=user_id,
                    visibility="project",
                )
                if (entry_type or "").strip().lower() == "decision":
                    index_decision(
                        self.db,
                        project_id=project_id,
                        decision_id=entry.id,
                        name=(content or "decision")[:80],
                        summary=content,
                        owner_user_id=user_id,
                        visibility="project",
                    )
        except Exception:
            logger.debug("ProjectMemory registry sync failed (non-fatal)", exc_info=True)

        logger.info(
            "ProjectMemory written | project=%s type=%s agent=%s",
            project_id, entry_type, agent_app_id,
        )
        return entry

    # ── read / context assembly ───────────────────────────────────────

    def read_project_context(
        self,
        project_id: str,
        limit: int = 20,
        min_importance: int = -5,
    ) -> list[ProjectMemory]:
        """Return the top-N most relevant entries for prompt injection.

        Ranking formula: ``importance * 2 + recency_bonus + usage_count``.

        *recency_bonus*: +3 if accessed within last hour, +2 within 24h,
        +1 within 7 days, 0 otherwise.

        Results are ordered by score descending, then by created_date desc.
        """
        now = datetime.now(timezone.utc)
        cutoff_1h = now - timedelta(hours=1)
        cutoff_24h = now - timedelta(hours=24)
        cutoff_7d = now - timedelta(days=7)

        entries = self.db.query(ProjectMemory).filter(
            ProjectMemory.project_id == project_id,
            ProjectMemory.is_deleted == False,
        ).all()

        # In-memory TTL filter (SQLite-compatible — no make_interval dependency)
        def _ttl_filter(e: ProjectMemory) -> bool:
            if e.ttl_days is None:
                return True
            if e.created_date is None:
                return True
            return e.created_date >= now - timedelta(days=e.ttl_days)

        entries = [e for e in entries if _ttl_filter(e)]

        def _score(e: ProjectMemory) -> float:
            recency = 0
            if e.last_accessed_at:
                if e.last_accessed_at >= cutoff_1h:
                    recency = 3
                elif e.last_accessed_at >= cutoff_24h:
                    recency = 2
                elif e.last_accessed_at >= cutoff_7d:
                    recency = 1
            return (e.importance or 0) * 2 + recency + (e.usage_count or 0)

        entries.sort(key=_score, reverse=True)

        result = entries[:limit]
        # Update last_accessed_at for returned entries
        for entry in result:
            entry.last_accessed_at = now
        self.db.flush()
        return result

    def format_snapshot(self, entries: list[ProjectMemory]) -> str:
        """Format project memory entries into a single text block for prompt injection."""
        if not entries:
            return ""

        lines = ["<project_memory>"]
        for e in entries:
            lines.append(f"  [{e.entry_type}] (importance={e.importance}) {e.content}")
        lines.append("</project_memory>")

        snapshot = "\n".join(lines)

        # Enforce char limit
        if len(snapshot) > PROJECT_MEMORY_CHAR_LIMIT:
            # Trim from the bottom (lowest-ranked entries)
            header = "<project_memory>\n"
            footer = "\n</project_memory>"
            max_body = PROJECT_MEMORY_CHAR_LIMIT - len(header) - len(footer)
            body = "\n".join(lines[1:-1])
            # We keep the last-segment up to max_body chars
            truncated = body[:max_body]
            snapshot = f"{header}{truncated}{footer}"

        return snapshot

    # ── utility ───────────────────────────────────────────────────────

    def delete_entry(self, entry_id: str) -> bool:
        """Soft-delete a project memory entry."""
        entry = self.db.query(ProjectMemory).filter(
            ProjectMemory.id == entry_id,
        ).first()
        if not entry:
            return False
        entry.is_deleted = True
        self.db.flush()
        return True

    def search_entries(
        self,
        project_id: str,
        query: str,
        limit: int = 10,
    ) -> list[ProjectMemory]:
        """Simple content-based search within a project's memory."""
        return self.db.query(ProjectMemory).filter(
            ProjectMemory.project_id == project_id,
            ProjectMemory.is_deleted == False,
            ProjectMemory.content.ilike(f"%{query}%"),
        ).order_by(
            ProjectMemory.importance.desc(),
            ProjectMemory.created_date.desc(),
        ).limit(limit).all()
