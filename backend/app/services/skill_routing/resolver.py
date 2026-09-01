"""Deterministic priority-based skill resolver.

The SkillResolver computes which skill should own a turn *before* the LLM
reasons, recording the result in a ``RoutingDecision`` dataclass that is
enforced across plan_dag and finalize.

Priority pipeline (highest to lowest):

1. **explicit_invoke** – user explicitly picked/typed a skill
2. **exclusive_override** – picked custom skill declares ``exclusive=True``
   → suppress ALL default handling
3. **format_intent** – ``detect_file_intent`` finds an explicit format
   → auto-pick the built-in default for that format
4. **soft_intent** – heuristic soft-intent detection
5. **llm_catalog_pick** – nothing matches → return an empty chosen_skill
   so the LLM picks from the full skill catalog via the Skill meta-tool /
   SkillPlannerHook. ``FALLBACK_SKILL`` ("docx") remains defined as the
   absolute last resort for callers that need a guaranteed artifact type.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from app.services.skill_routing.namespace import parse_command, SOURCE_TIERS

logger = logging.getLogger(__name__)

# ── built-in Zhanlu default skills (format → default skill name) ────────
DEFAULT_SKILL_MAP: dict[str, str] = {
    "docx": "docx",
    "pptx": "ppt-design",  # FIX 2026-08-23: use professional design skill instead of bare python-pptx
    "pdf": "pdf",
    "html": "artifacts-builder",
    # "dashboard-generation" skill now carries the FULL-STACK dashboard
    # methodology: uiux_design_system(--persist) → data contract → 
    # create_fullstack_dashboard → WebSocket live data. Legacy SQL-widget
    # creation lives behind LEGACY_DASHBOARD_ENABLED only.
    "dashboard": "dashboard-generation",
    # Companion design intelligence — explicit /design, /uiux, /palette
    # commands resolve here. Does NOT auto-trigger from soft-intent
    # (handled separately by default_skills._build_default_skills_block).
    "design": "ui-ux-pro-max",
    "uiux": "ui-ux-pro-max",
    "palette": "ui-ux-pro-max",
}

# Companion skill: ui-ux-pro-max is a *helper* skill, not a default format.
# Surfaced as a sidekick via the /design, /uiux, /palette commands above
# (see agent_prompts._build_default_skills_block). It is ALSO the mandatory
# design-first step of the full-stack dashboard pipeline
# (uiux_design_system --persist → design_system_ref → create_fullstack_dashboard).
COMPANION_SKILLS: frozenset[str] = frozenset({"ui-ux-pro-max"})

FALLBACK_SKILL = "docx"


@dataclass
class RoutingDecision:
    """The output of ``SkillResolver.resolve()``.

    Carries enough information for downstream modules (plan_dag, finalize,
    task_spec) to enforce the routing choice without additional lookups.
    """

    chosen_skill: str
    """Bare skill name, e.g. ``"pptx"`` or ``"my-template-ppt"``."""

    namespace: str
    """Fully-qualified ``source:name``, e.g. ``"builtin:pptx"``."""

    source: str
    """Source tier: ``"builtin"``, ``"user"``, ``"marketplace"``, ``"generated"``."""

    is_default: bool
    """True when a built-in Zhanlu default was auto-picked."""

    exclusive: bool
    """When True, ALL default handling (hints, auto-export) is suppressed."""

    allow_default_fallback: bool
    """When True, defaults may be used as backup if the primary skill fails."""

    reason: str
    """Why this decision was made.  One of:
    ``explicit_invoke``, ``exclusive_override``, ``format_intent``,
    ``soft_intent``, ``llm_catalog_pick``.
    """

    bypassed_defaults: list[str] = field(default_factory=list)
    """Default skills suppressed by this decision (for logging/audit)."""


class SkillResolver:
    """Deterministic priority-pipeline skill resolver.

    Usage::

        resolver = SkillResolver()
        decision = resolver.resolve(
            user_message="Make a sales report PPT",
            picked_skill=None,     # no explicit skill → routes to built-in pptx
            db=db_session,
        )
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(
        self,
        user_message: str,
        picked_skill: Optional[dict] = None,
        db=None,
    ) -> RoutingDecision:
        """Run the deterministic priority pipeline and return a decision.

        Parameters
        ----------
        user_message:
            The raw user message for format / soft-intent detection.
        picked_skill:
            A skill dict with keys ``name``, ``source``, ``exclusive``, and
            optionally ``fallback_allowed``.  ``None`` means no skill was
            explicitly picked (generic request).
        db:
            Optional SQLAlchemy Session for DB-backed skill lookups.

        Returns
        -------
        RoutingDecision
        """
        # ── Tier 1: explicit user invocation ──────────────────────────
        if picked_skill and picked_skill.get("name"):
            return self._resolve_explicit(picked_skill)

        # ── Tier 2–4: auto-pick defaults by format / soft-intent ──────
        return self._resolve_default(user_message)

    # ------------------------------------------------------------------
    # Pipeline tiers
    # ------------------------------------------------------------------

    def _resolve_explicit(self, picked_skill: dict) -> RoutingDecision:
        """User explicitly picked/typed a skill."""
        name = picked_skill["name"]
        source = picked_skill.get("source", "user")
        namespace = f"{source}:{name}"
        exclusive = bool(picked_skill.get("exclusive", False))
        allow_fallback = bool(picked_skill.get("fallback_allowed", True))

        reason = "exclusive_override" if exclusive else "explicit_invoke"

        return RoutingDecision(
            chosen_skill=name,
            namespace=namespace,
            source=source,
            is_default=False,
            exclusive=exclusive,
            allow_default_fallback=allow_fallback,
            reason=reason,
            bypassed_defaults=list(DEFAULT_SKILL_MAP.values()) if exclusive else [],
        )

    def _resolve_default(self, user_message: str) -> RoutingDecision:
        """Auto-pick a built-in default based on format/soft-intent."""
        # ── Tier 3: explicit format intent ────────────────────────────
        file_intent = self._detect_file_intent(user_message)
        if file_intent and file_intent in DEFAULT_SKILL_MAP:
            skill_name = DEFAULT_SKILL_MAP[file_intent]
            return RoutingDecision(
                chosen_skill=skill_name,
                namespace=f"builtin:{skill_name}",
                source="builtin",
                is_default=True,
                exclusive=False,
                allow_default_fallback=True,
                reason="format_intent",
                bypassed_defaults=[],
            )

        # ── Tier 4: soft-intent heuristic ─────────────────────────────
        soft = self._detect_soft_intent(user_message)
        if soft and soft in DEFAULT_SKILL_MAP:
            skill_name = DEFAULT_SKILL_MAP[soft]
            return RoutingDecision(
                chosen_skill=skill_name,
                namespace=f"builtin:{skill_name}",
                source="builtin",
                is_default=True,
                exclusive=False,
                allow_default_fallback=True,
                reason="soft_intent",
                bypassed_defaults=[],
            )

        # ── Tier 5: no deterministic match → let the LLM pick ─────────
        # Return an empty chosen_skill so downstream callers that check
        # ``if chosen_skill:`` skip the forced-default path. The LLM then
        # selects from the full skill catalog injected via the Skill
        # meta-tool / SkillPlannerHook. FALLBACK_SKILL remains available
        # as the absolute last resort for callers that require a
        # guaranteed artifact type.
        return RoutingDecision(
            chosen_skill="",
            namespace="",
            source="builtin",
            is_default=False,
            exclusive=False,
            allow_default_fallback=True,
            reason="llm_catalog_pick",
            bypassed_defaults=[],
        )

    # ------------------------------------------------------------------
    # Reused detection helpers (thin wrappers around existing modules)
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_file_intent(user_message: str) -> Optional[str]:
        """Reuse ``intent_router.detect_file_intent``."""
        try:
            from app.services.synexia.intent_router import detect_file_intent
            return detect_file_intent(user_message)
        except Exception:
            logger.warning("detect_file_intent unavailable", exc_info=True)
            return None

    @staticmethod
    def _detect_soft_intent(user_message: str) -> Optional[str]:
        """Reuse ``default_skills.detect_soft_intent``."""
        try:
            from app.services.synexia.default_skills import detect_soft_intent
            return detect_soft_intent(user_message)
        except Exception:
            logger.warning("detect_soft_intent unavailable", exc_info=True)
            return None
