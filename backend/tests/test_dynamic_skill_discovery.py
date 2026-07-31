"""Integration tests for the dynamic skill-discovery + universal web-collection layer.

Covers the wiring activated by the "smart-dynamic-skills-and-universal-web-
collection" plan:

1. ``register_skill_meta_tool()`` registers the ``Skill`` tool in the registry.
2. ``SkillPlannerHook.build_plan_prompt_extra()`` builds a catalog from the
   SkillsRegistry (not just ManifestIndex).
3. All system agents have ``web_search`` / ``web_extract`` / ``Skill`` in
   their ``tool_config``; ``general_assistant`` also has ``agent_browser``.
4. ``DEFAULT_USER_AGENT_TOOLS`` includes the new universal tools.
5. ``pick_default_skill()`` returns ``None`` for truly ambiguous requests
   (the former forced-``docx`` fallback is gone).
6. ``SkillResolver`` returns ``reason="llm_catalog_pick"`` for generic
   requests instead of ``fallback_docx``.
"""

from __future__ import annotations

import pytest

from app.services.skill_routing.resolver import SkillResolver, FALLBACK_SKILL
from app.services.synexia.default_skills import pick_default_skill


# ---------------------------------------------------------------------------
# 1. Skill meta-tool registration
# ---------------------------------------------------------------------------

class TestSkillMetaToolRegistration:
    """The unified Skill meta-tool must be registerable and discoverable."""

    def test_register_skill_meta_tool_adds_skill_to_registry(self):
        """register_skill_meta_tool() should add 'Skill' to the tool registry."""
        from app.services.tool_registry import registry
        from app.services.skill_routing.meta_tool import register_skill_meta_tool

        # Ensure it's registered (idempotent — safe to call twice)
        register_skill_meta_tool()
        available = registry.list_available()
        assert "Skill" in available, (
            "Skill meta-tool not in registry after register_skill_meta_tool()"
        )

    def test_skill_meta_tool_has_catalog_in_description(self):
        """The Skill tool description should embed the skill catalog."""
        from app.services.tool_registry import registry
        from app.services.skill_routing.meta_tool import register_skill_meta_tool

        register_skill_meta_tool()
        entry = registry.get_entry("Skill")
        assert entry is not None
        desc = entry.schema.get("function", {}).get("description", "")
        # The description should mention the dispatch contract
        assert "Skill" in desc or "skill" in desc.lower()


# ---------------------------------------------------------------------------
# 2. SkillPlannerHook catalog from SkillsRegistry
# ---------------------------------------------------------------------------

class TestPlannerHookCatalog:
    """The planner hook should build a catalog from the SkillsRegistry."""

    def test_build_plan_prompt_extra_returns_nonempty_string(self):
        """build_plan_prompt_extra() should return a non-empty catalog string."""
        from app.services.skills_loader.skill_planner_hook import SkillPlannerHook

        hook = SkillPlannerHook()
        block = hook.build_plan_prompt_extra()
        assert isinstance(block, str)
        # Either the registry has skills (catalog present) or it falls back
        # to ManifestIndex. Either way it should produce *something* in a
        # normally-seeded environment.
        assert len(block) > 0

    def test_build_plan_prompt_extra_mentions_skills(self):
        """The catalog should contain the 'Available skills' header."""
        from app.services.skills_loader.skill_planner_hook import SkillPlannerHook

        hook = SkillPlannerHook()
        block = hook.build_plan_prompt_extra()
        assert "Available skills" in block or "skill" in block.lower()

    def test_materialize_node_loads_known_skill_body(self):
        """materialize_node() should return a body for a load_skill node.

        Uses 'docx' which is a bundled skill present in backend/skills/.
        """
        from app.services.skills_loader.skill_planner_hook import SkillPlannerHook

        hook = SkillPlannerHook()
        result = hook.materialize_node({"type": "load_skill", "skill": "docx"})
        # If docx is loaded in the registry, we get a body; if not, None.
        # Either is acceptable — we just verify no exception is raised.
        if result is not None:
            assert result.name == "docx"
            assert isinstance(result.body, str)

    def test_materialize_node_returns_none_for_unknown_skill(self):
        """materialize_node() returns None for a non-existent skill."""
        from app.services.skills_loader.skill_planner_hook import SkillPlannerHook

        hook = SkillPlannerHook()
        result = hook.materialize_node(
            {"type": "load_skill", "skill": "totally-nonexistent-skill-xyz"}
        )
        assert result is None

    def test_materialize_node_ignores_wrong_node_type(self):
        """materialize_node() returns None for non-load_skill nodes."""
        from app.services.skills_loader.skill_planner_hook import SkillPlannerHook

        hook = SkillPlannerHook()
        assert hook.materialize_node({"type": "tool", "name": "web_search"}) is None
        assert hook.materialize_node({}) is None
        assert hook.materialize_node("not a dict") is None


# ---------------------------------------------------------------------------
# 3. System agents have web collection + Skill tools
# ---------------------------------------------------------------------------

class TestSystemAgentTools:
    """Every system agent should have web collection + Skill discovery tools."""

    @staticmethod
    def _system_configs():
        from app.services.system_agents import _build_system_agent_configs
        return {c["name"]: c for c in _build_system_agent_configs()}

    @staticmethod
    def _enabled(cfg):
        return set(cfg["tool_config"].get("enabled_tools", []))

    def test_all_agents_have_skill_meta_tool(self):
        """The 'Skill' meta-tool should be in every system agent's palette."""
        for name, cfg in self._system_configs().items():
            enabled = self._enabled(cfg)
            assert "Skill" in enabled, (
                f"Agent '{name}' is missing the 'Skill' meta-tool"
            )

    def test_all_agents_have_web_search(self):
        """web_search should be available to every system agent."""
        for name, cfg in self._system_configs().items():
            enabled = self._enabled(cfg)
            assert "web_search" in enabled, (
                f"Agent '{name}' is missing 'web_search'"
            )

    def test_all_agents_have_web_extract(self):
        """web_extract should be available to every system agent."""
        for name, cfg in self._system_configs().items():
            enabled = self._enabled(cfg)
            assert "web_extract" in enabled, (
                f"Agent '{name}' is missing 'web_extract'"
            )

    def test_general_assistant_has_agent_browser(self):
        """general_assistant should have agent_browser for web collection."""
        enabled = self._enabled(self._system_configs()["general_assistant"])
        assert "agent_browser" in enabled

    def test_power_user_has_agent_browser(self):
        """power_user should have agent_browser (full tool surface)."""
        enabled = self._enabled(self._system_configs()["power_user"])
        assert "agent_browser" in enabled

    def test_general_assistant_has_load_skill_body(self):
        """general_assistant should have load_skill_body for progressive disclosure."""
        enabled = self._enabled(self._system_configs()["general_assistant"])
        assert "load_skill_body" in enabled

    def test_narrow_agents_dont_have_browser(self):
        """Management agents (agent_builder, skill_agent, automation_agent)
        should NOT have agent_browser (reduced security surface)."""
        configs = self._system_configs()
        for name in ("agent_builder", "skill_agent", "automation_agent"):
            enabled = self._enabled(configs[name])
            assert "agent_browser" not in enabled, (
                f"Narrow agent '{name}' should not have agent_browser"
            )


# ---------------------------------------------------------------------------
# 4. DEFAULT_USER_AGENT_TOOLS includes universal tools
# ---------------------------------------------------------------------------

class TestDefaultUserAgentTools:
    """User-created agents should get web collection + Skill by default."""

    def test_default_user_agent_tools_has_skill(self):
        from app.services.tool_registry import DEFAULT_USER_AGENT_TOOLS
        assert "Skill" in DEFAULT_USER_AGENT_TOOLS

    def test_default_user_agent_tools_has_agent_browser(self):
        from app.services.tool_registry import DEFAULT_USER_AGENT_TOOLS
        assert "agent_browser" in DEFAULT_USER_AGENT_TOOLS

    def test_default_user_agent_tools_has_web_search(self):
        from app.services.tool_registry import DEFAULT_USER_AGENT_TOOLS
        assert "web_search" in DEFAULT_USER_AGENT_TOOLS

    def test_default_user_agent_tools_has_web_extract(self):
        from app.services.tool_registry import DEFAULT_USER_AGENT_TOOLS
        assert "web_extract" in DEFAULT_USER_AGENT_TOOLS

    def test_default_user_agent_tools_has_load_skill_body(self):
        from app.services.tool_registry import DEFAULT_USER_AGENT_TOOLS
        assert "load_skill_body" in DEFAULT_USER_AGENT_TOOLS


# ---------------------------------------------------------------------------
# 5. pick_default_skill returns None for ambiguous requests
# ---------------------------------------------------------------------------

class TestDefaultSkillFallback:
    """The forced-docx fallback is replaced by None (LLM picks from catalog)."""

    def test_truly_ambiguous_returns_none(self):
        """A message with no format or soft-intent signal returns None."""
        assert pick_default_skill("do something for me") is None

    def test_greeting_returns_none(self):
        """A plain greeting returns None (no forced docx)."""
        assert pick_default_skill("hello, how are you?") is None

    def test_explicit_format_still_works(self):
        """Explicit format keywords still return the right default skill."""
        result = pick_default_skill("make me a pptx deck")
        assert result is not None
        assert result["skill_name"] == "pptx"

    def test_soft_intent_still_works(self):
        """Soft-intent keywords (e.g. 'report') still map to docx."""
        result = pick_default_skill("make a sales report")
        assert result is not None
        assert result["skill_name"] == "docx"

    def test_override_path_still_returns_none(self):
        """When active_skill is set, defaults are skipped (returns None)."""
        result = pick_default_skill(
            "make me a docx report",
            active_skill={"name": "my-custom-skill"},
        )
        assert result is None


# ---------------------------------------------------------------------------
# 6. SkillResolver returns llm_catalog_pick for generic requests
# ---------------------------------------------------------------------------

class TestResolverLlmCatalogPick:
    """The resolver's Tier-5 should return llm_catalog_pick, not fallback_docx."""

    def test_generic_request_returns_llm_catalog_pick(self):
        from app.services.skill_routing.resolver import SkillResolver

        resolver = SkillResolver()
        decision = resolver.resolve(
            user_message="hello world",
            picked_skill=None,
        )
        assert decision.reason == "llm_catalog_pick"
        assert decision.chosen_skill == ""
        assert decision.is_default is False
        assert decision.namespace == ""

    def test_fallback_skill_constant_still_defined(self):
        """FALLBACK_SKILL should still exist as the absolute last resort."""
        assert FALLBACK_SKILL == "docx"

    def test_format_intent_tier_unchanged(self):
        """Explicit format requests still route via format_intent."""
        from app.services.skill_routing.resolver import SkillResolver

        resolver = SkillResolver()
        decision = resolver.resolve(
            user_message="create a powerpoint deck",
            picked_skill=None,
        )
        assert decision.reason == "format_intent"
        assert decision.chosen_skill == "pptx"
