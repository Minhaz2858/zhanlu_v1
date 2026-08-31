"""Tests for the turn-level skill catalog context (runtime_catalog) and the
FIX 2026-08-29 tool-registration changes.

Covers:
1. catalog.build_catalog always_include: force-included names survive
   budget truncation.
2. catalog_budget_for_window: context-window scaling with floor/ceiling.
3. runtime build_skill_catalog_context: a PPT request auto-routes to
   ppt-design, forces the search hits into the catalog, and injects the
   routed skill's SKILL.md body as an Active Skill directive.
4. Tool registration: importing tool_handlers registers BOTH
   ``load_skill_body`` and the ``Skill`` meta-tool (previously
   load_skill_body was never imported → "Unknown tool: load_skill_body"
   in real chat traces).
5. Prompt-truth guard: the instruction block never names a phantom tool —
   every tool it references must exist in the registry.
"""

import os
import sys
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)


# ==========================================================================
# 1. Catalog force-include
# ==========================================================================


class TestCatalogAlwaysInclude(unittest.TestCase):
    def _skills(self):
        return [
            {"name": "a", "source": "builtin", "description": "skill a"},
            {"name": "b", "source": "builtin", "description": "skill b"},
            {"name": "c", "source": "builtin", "description": "skill c"},
            {"name": "d", "source": "builtin", "description": "skill d"},
        ]

    def test_forced_names_survive_tiny_budget(self):
        from app.services.skill_routing.catalog import build_catalog

        # Budget big enough for ~2 entries: without force-include, only the
        # top-priority entries fit. With force-include, b/c must be present.
        block = build_catalog(
            self._skills(),
            budget_chars=700,
            always_include=["b", "c"],
        )
        for name in ("b", "c"):
            self.assertIn(f"<name>{name}</name>", block)

    def test_forced_names_first_and_deduped(self):
        from app.services.skill_routing.catalog import build_catalog

        block = build_catalog(
            self._skills(),
            budget_chars=10_000,
            always_include=["c", "a", "c"],  # duplicate c
        )
        self.assertLess(block.index("<name>c</name>"), block.index("<name>a</name>"))
        self.assertEqual(block.count("<name>c</name>"), 1)

    def test_unknown_forced_name_ignored(self):
        from app.services.skill_routing.catalog import build_catalog

        block = build_catalog(
            self._skills(),
            budget_chars=10_000,
            always_include=["nope"],
        )
        self.assertNotIn("nope", block)
        self.assertIn("<name>a</name>", block)

    def test_no_always_include_is_backward_compatible(self):
        from app.services.skill_routing.catalog import build_catalog

        block = build_catalog(self._skills(), budget_chars=10_000)
        self.assertEqual(block.count("<skill>"), 4)


# ==========================================================================
# 2. Budget scaling
# ==========================================================================


class TestCatalogBudget(unittest.TestCase):
    def test_none_uses_floor(self):
        from app.services.skill_routing.runtime_catalog import catalog_budget_for_window

        self.assertEqual(catalog_budget_for_window(None), 15_000)

    def test_small_window_floor(self):
        from app.services.skill_routing.runtime_catalog import catalog_budget_for_window

        self.assertEqual(catalog_budget_for_window(8_000), 15_000)

    def test_large_window_scales(self):
        from app.services.skill_routing.runtime_catalog import catalog_budget_for_window

        # 128k window → 32k chars; 200k → capped at 40k
        self.assertEqual(catalog_budget_for_window(128_000), 32_000)
        self.assertEqual(catalog_budget_for_window(200_000), 40_000)


# ==========================================================================
# 3. Runtime catalog context (integration)
# ==========================================================================


class TestBuildSkillCatalogContext(unittest.TestCase):
    def test_ppt_request_routes_and_injects_body(self):
        from app.services.skill_routing.runtime_catalog import (
            build_skill_catalog_context,
        )

        block = build_skill_catalog_context(
            "make a c5 c9 market view ppt",
            db=None,
            context_window_tokens=128_000,
        )
        self.assertTrue(block)
        # Routed default skill (format_intent → ppt-design) must be present
        # in the catalog block AND have its body injected as a directive.
        self.assertIn("## Active Skill (auto-routed)", block)
        self.assertIn("ppt-design", block)
        # The catalog block must be present with the XML wrapper.
        self.assertIn("<available_skills>", block)
        self.assertIn("</available_skills>", block)

    def test_truthful_tool_instructions(self):
        import app.services.tool_handlers  # noqa: F401  (populate registry)
        from app.services.skill_routing.runtime_catalog import (
            build_skill_catalog_context,
        )
        from app.services.tool_registry import registry

        block = build_skill_catalog_context("make a report", db=None)
        # Primary path: the `skills` tool with action=search/load.
        self.assertIn("`skills` tool", block)
        self.assertIn('"action": "load"', block)
        # Every tool NAMED in the block must be registered. The old prompt
        # promised load_skill_body / Skill when they were not registered.
        registered = set(registry.list_names()) | set(registry.list_available())
        for name in ("load_skill_body", "Skill", "skills"):
            # The block may or may not mention the dedicated tools depending
            # on registration — but if it mentions them, they MUST exist.
            if f"`{name}`" in block or f"`{name}` " in block:
                self.assertIn(name, registered, f"phantom tool {name} in prompt")


# ==========================================================================
# 4. Tool registration (the root-cause fix)
# ==========================================================================


class TestSkillToolRegistration(unittest.TestCase):
    def test_load_skill_body_and_skill_registered(self):
        import app.services.tool_handlers  # noqa: F401  (registers tools)
        from app.services.tool_registry import registry

        names = set(registry.list_names()) | set(registry.list_available())
        self.assertIn("load_skill_body", names)
        self.assertIn("Skill", names)
        self.assertIn("skills", names)

    def test_load_skill_body_handler_resolves(self):
        import app.services.tool_handlers  # noqa: F401
        from app.services.tool_registry import registry

        self.assertIsNotNone(registry.get_handler("load_skill_body"))
        self.assertIsNotNone(registry.get_handler("skills"))


if __name__ == "__main__":
    unittest.main()
