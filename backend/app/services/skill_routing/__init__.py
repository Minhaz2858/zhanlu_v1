"""Priority-based skill routing system for the Zhanlu AI agent.

Provides a deterministic, inspectable skill-routing layer that sits between
a user message and the agent execution pipeline.  Inspired by the Claude
Agent Skills architecture, it implements:

1. A deterministic central SkillResolver (priority pipeline)
2. A Skill meta-tool with progressive disclosure
3. A token-budgeted skill catalog injected per turn
4. Namespaced collision handling (``source:name``)

Public API
----------
.. code-block:: python

    from app.services.skill_routing import (
        # Dataclasses
        RoutingDecision,

        # Resolver (deterministic priority pipeline)
        SkillResolver,

        # Catalog builder (token-budgeted, progressive-disclosure)
        build_catalog,

        # Namespace utilities
        parse_command,
        resolve_collision,
        to_namespaced,
        SOURCE_TIERS,

        # Meta-tool registration (called at app startup)
        register_skill_meta_tool,

        # Post-router hook (forces Skill invocation on strong matches)
        post_router_pick,
        score_skill_match,
        STRONG_MATCH_THRESHOLD,
    )
"""

from __future__ import annotations

from app.services.skill_routing.resolver import RoutingDecision, SkillResolver
from app.services.skill_routing.catalog import build_catalog
from app.services.skill_routing.namespace import (
    parse_command,
    resolve_collision,
    to_namespaced,
    SOURCE_TIERS,
)
from app.services.skill_routing.meta_tool import register_skill_meta_tool
from app.services.skill_routing.post_router_hook import (
    STRONG_MATCH_THRESHOLD,
    build_agent_forced_skill_directive,
    post_router_pick,
    score_skill_match,
)

__all__ = [
    "RoutingDecision",
    "SkillResolver",
    "build_catalog",
    "parse_command",
    "resolve_collision",
    "to_namespaced",
    "SOURCE_TIERS",
    "register_skill_meta_tool",
    "post_router_pick",
    "score_skill_match",
    "STRONG_MATCH_THRESHOLD",
    "build_agent_forced_skill_directive",
]
