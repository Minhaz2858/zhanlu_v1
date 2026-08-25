"""Dataclasses for the ProjectKnowledgeCache facade."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

CacheStatusKind = Literal["pending", "indexing", "ready", "partial", "error"]
CacheQueryKind = Literal["product", "entity", "metric"]


@dataclass
class CacheStatus:
    """Result of an ingestion run for one (project_id, kb_id) pair."""

    status: CacheStatusKind = "pending"
    tables: int = 0
    entities: int = 0
    links: int = 0
    error: str | None = None
    elapsed_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "tables": self.tables,
            "entities": self.entities,
            "links": self.links,
            "error": self.error,
            "elapsed_s": self.elapsed_s,
        }


@dataclass
class CacheStats:
    """Read-only snapshot of cache health for one project."""

    project_id: str
    entities: int = 0
    links: int = 0
    metrics: int = 0
    overlays: int = 0
    catalog_tables: int = 0
    last_ingest_at: str | None = None
    last_status: CacheStatusKind = "pending"

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "entities": self.entities,
            "links": self.links,
            "metrics": self.metrics,
            "overlays": self.overlays,
            "catalog_tables": self.catalog_tables,
            "last_ingest_at": self.last_ingest_at,
            "last_status": self.last_status,
        }


@dataclass
class CacheQueryResult:
    """A hit from the cache that can be injected into the agent context."""

    kind: CacheQueryKind
    data: dict = field(default_factory=dict)
    # human-readable block to drop into the LLM context
    context_block: str = ""
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "data": self.data,
            "context_block": self.context_block,
            "confidence": self.confidence,
        }
