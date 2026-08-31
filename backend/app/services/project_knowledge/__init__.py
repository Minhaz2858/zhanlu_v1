"""Project-scoped knowledge cache (generic — serves any project-bound agent).

This module composes existing tables (project_entity, project_entity_link,
project_catalog_overlay, project_metric, kb_table_meta, kb_column_meta,
kb_table_relation, resource_registry) into a single facade that:

- runs ingestion whenever a KnowledgeBase is bound to a project-scoped agent
- answers questions via 3 layered lookups (resolver -> entity -> metric) before
  the agent's LLM is called (Qwen fast-path)

All operations are strictly project-scoped. The module is gated by
``PROJECT_KNOWLEDGE_CACHE_ENABLED`` (default False). Every layer fails
open; the LLM is always the safety net.
"""
from __future__ import annotations

from .cache import ProjectKnowledgeCache
from .models import CacheStatus, CacheStats, CacheQueryResult

__all__ = [
    "ProjectKnowledgeCache",
    "CacheStatus",
    "CacheStats",
    "CacheQueryResult",
]
