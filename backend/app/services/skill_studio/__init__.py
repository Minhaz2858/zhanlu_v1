"""Smart Skill Agent — Kimi-equivalent skill studio.

This package turns the Skill Agent from a single-file SKILL.md builder into a
folder-style skill studio:

- ``intent_router``        — always-on classification of each turn: create /
  use / edit / normal chat (keyword fast-path + optional LLM fallback).
- ``creation_orchestrator``— stateful 4-phase flow: Understand -> Propose ->
  Draft -> Save, producing a full folder package (SKILL.md + references/ +
  assets/templates/).
- ``draft_store``          — persistence for in-flight ``SkillDraft`` state,
  keyed by conversation_id.
- ``semantic_finder``      — embedding-based discovery with RRF fusion against
  the existing keyword search.

All behavior is gated behind ``SMART_SKILL_AGENT_ENABLED`` (master),
``SKILL_SEMANTIC_SEARCH_ENABLED``, and ``SKILL_TEMPLATE_REUSE_ENABLED`` — all
default OFF so existing flat-file skills continue to work unchanged.
"""

from app.services.skill_studio.intent_router import (
    SkillIntent,
    SkillIntentRouter,
)
from app.services.skill_studio.draft_store import SkillDraft, SkillDraftStore
from app.services.skill_studio.creation_orchestrator import CreationOrchestrator
from app.services.skill_studio.semantic_finder import (
    SkillSearchResult,
    semantic_search,
)

__all__ = [
    "SkillIntent",
    "SkillIntentRouter",
    "SkillDraft",
    "SkillDraftStore",
    "CreationOrchestrator",
    "SkillSearchResult",
    "semantic_search",
]
