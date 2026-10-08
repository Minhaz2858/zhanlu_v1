"""Versioned prompt-block registry (P2-7, IBM iterate-and-refine).

Central inventory of every shared system-prompt block used by the
``get_system_prompt`` assembly funnel. Each entry carries a semantic
version that MUST be bumped whenever the block's text changes — this is
the key the prompt A/B harness (P2-8) keys results on, and the token
accounting (P0-2) reads sizes from.

The block TEXT still lives in ``agent_prompts.py`` for now (moving the
~500 lines of literals is a mechanical follow-up); this registry is the
seam: once blocks live in their own modules, only the getters change.

Usage:
    from app.services.prompts.registry import get_block, iter_blocks
    block, version = get_block("conversation_tone")
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _agent_prompts():
    # Lazy: importing agent_prompts pulls tool schemas + skills blocks.
    from app.services import agent_prompts as ap

    return ap


def _conversation_tone() -> str:
    return _agent_prompts()._CONVERSATION_TONE_BLOCK


def _initiative() -> str:
    return _agent_prompts()._INITIATIVE_BLOCK


def _runtime_date_anchor() -> str:
    return _agent_prompts()._runtime_context_block()


def _answer_verification() -> str:
    return _agent_prompts()._ANSWER_VERIFICATION_BLOCK


def _schema_aware_protocol() -> str:
    return _agent_prompts()._SCHEMA_AWARE_PROTOCOL_BLOCK


def _research_analyst_directive() -> str:
    return _agent_prompts()._RESEARCH_ANALYST_DIRECTIVE


def _entity_master_filter() -> str:
    return _agent_prompts()._ENTITY_MASTER_FILTER_BLOCK


def _grounding_required() -> str:
    return _agent_prompts().GROUNDING_REQUIRED_BLOCK


class BlockSpec:
    """One shared system-prompt block: identity + version + lazy getter."""

    def __init__(
        self,
        name: str,
        version: int,
        description: str,
        getter: Callable[[], str],
    ) -> None:
        self.name = name
        self.version = version
        self.description = description
        self._getter = getter
        self._cached: str | None = None

    def text(self) -> str:
        if self._cached is None:
            self._cached = self._getter() or ""
        return self._cached

    def tokens(self) -> int:
        """~4 chars/token heuristic (same convention as context_assembler)."""
        return max(1, len(self.text()) // 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "tokens_approx": self.tokens(),
        }


BLOCK_REGISTRY: dict[str, BlockSpec] = {
    spec.name: spec
    for spec in (
        BlockSpec(
            "conversation_tone", 1,
            "Universal warm/natural tone block appended to every agent.",
            _conversation_tone,
        ),
        BlockSpec(
            "initiative", 1,
            "Action-bias block appended to every agent.",
            _initiative,
        ),
        BlockSpec(
            "runtime_date_anchor", 1,
            "Date-only current-date anchor (P0-1: no clock time — prefix-cache stable).",
            _runtime_date_anchor,
        ),
        BlockSpec(
            "answer_verification", 1,
            "Self-verify + replan protocol (SELF_EVAL_REPLAN_ENABLED).",
            _answer_verification,
        ),
        BlockSpec(
            "schema_aware_protocol", 1,
            "Multi-table join/FK self-correction protocol (SCHEMA_GRAPH_ENABLED, db-bound).",
            _schema_aware_protocol,
        ),
        BlockSpec(
            "research_analyst_directive", 1,
            "Institutional-grade analysis depth directive (db-bound, market profile flag).",
            _research_analyst_directive,
        ),
        BlockSpec(
            "entity_master_filter", 1,
            "Master-first query pattern (ENTITY_MASTER_FILTER_ENABLED, db-bound).",
            _entity_master_filter,
        ),
        BlockSpec(
            "grounding_required", 1,
            "Per-message [GROUNDING REQUIRED] re-emphasis (time-sensitive/URL/file turns).",
            _grounding_required,
        ),
    )
}


def get_block(name: str) -> tuple[str, int]:
    """Return ``(text, version)`` for a registry block, or ("", 0)."""
    spec = BLOCK_REGISTRY.get(name)
    if spec is None:
        return "", 0
    return spec.text(), spec.version


def iter_blocks() -> list[BlockSpec]:
    return list(BLOCK_REGISTRY.values())


def registry_snapshot() -> list[dict[str, Any]]:
    """Diagnostics view (name, version, description, approx tokens)."""
    return [spec.to_dict() for spec in iter_blocks()]


__all__ = [
    "BLOCK_REGISTRY",
    "BlockSpec",
    "get_block",
    "iter_blocks",
    "registry_snapshot",
]
