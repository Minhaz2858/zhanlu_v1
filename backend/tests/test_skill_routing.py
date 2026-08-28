"""Tests for the priority-based skill routing system.

Covers:
1. namespace: parse_command, resolve_collision, SOURCE_TIERS ordering
2. resolver: priority pipeline (explicit → exclusive → format → soft → fallback)
3. catalog: token-budgeted progressive-disclosure block
4. integration: end-to-end routing decisions
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)


# ==========================================================================
# 1. Namespace utilities
# ==========================================================================


class TestParseCommand(unittest.TestCase):
    """namespace.parse_command must correctly split source:name."""

    def test_bare_name(self):
        from app.services.skill_routing.namespace import parse_command
        source, name = parse_command("pptx")
        self.assertIsNone(source)
        self.assertEqual(name, "pptx")

    def test_namespaced_user(self):
        from app.services.skill_routing.namespace import parse_command
        source, name = parse_command("user:my-template-ppt")
        self.assertEqual(source, "user")
        self.assertEqual(name, "my-template-ppt")

    def test_namespaced_builtin(self):
        from app.services.skill_routing.namespace import parse_command
        source, name = parse_command("builtin:pptx")
        self.assertEqual(source, "builtin")
        self.assertEqual(name, "pptx")

    def test_unknown_source_is_bare(self):
        """A colon with an unknown source prefix is treated as bare."""
        from app.services.skill_routing.namespace import parse_command
        source, name = parse_command("unknown:foo")
        self.assertIsNone(source)
        self.assertEqual(name, "unknown:foo")

    def test_multiple_colons_is_bare(self):
        """Only the first colon is inspected; 'a:b:c' has no known source."""
        from app.services.skill_routing.namespace import parse_command
        source, name = parse_command("a:b:c")
        self.assertIsNone(source)
        self.assertEqual(name, "a:b:c")

    def test_empty_string(self):
        from app.services.skill_routing.namespace import parse_command
        source, name = parse_command("")
        self.assertIsNone(source)
        self.assertEqual(name, "")


class TestToNamespaced(unittest.TestCase):
    def test_simple(self):
        from app.services.skill_routing.namespace import to_namespaced
        self.assertEqual(to_namespaced("builtin", "pptx"), "builtin:pptx")

    def test_user(self):
        from app.services.skill_routing.namespace import to_namespaced
        self.assertEqual(to_namespaced("user", "my-skill"), "user:my-skill")


class TestResolveCollision(unittest.TestCase):
    """namespace.resolve_collision must prefer user > marketplace > builtin."""

    def test_empty_candidates(self):
        from app.services.skill_routing.namespace import resolve_collision
        self.assertIsNone(resolve_collision("pptx", []))

    def test_single_candidate(self):
        from app.services.skill_routing.namespace import resolve_collision
        cands = [{"name": "pptx", "source": "builtin"}]
        result = resolve_collision("pptx", cands)
        self.assertEqual(result["source"], "builtin")

    def test_user_wins_over_builtin(self):
        from app.services.skill_routing.namespace import resolve_collision
        cands = [
            {"name": "pptx", "source": "builtin", "priority": 0},
            {"name": "pptx", "source": "user", "priority": 0},
        ]
        result = resolve_collision("pptx", cands)
        self.assertEqual(result["source"], "user")

    def test_marketplace_wins_over_builtin(self):
        from app.services.skill_routing.namespace import resolve_collision
        cands = [
            {"name": "pptx", "source": "builtin", "priority": 0},
            {"name": "pptx", "source": "marketplace", "priority": 0},
        ]
        result = resolve_collision("pptx", cands)
        self.assertEqual(result["source"], "marketplace")

    def test_higher_priority_wins_within_same_source(self):
        from app.services.skill_routing.namespace import resolve_collision
        cands = [
            {"name": "pptx", "source": "user", "priority": 10},
            {"name": "pptx", "source": "user", "priority": 100},
        ]
        result = resolve_collision("pptx", cands)
        self.assertEqual(result["priority"], 100)

    def test_user_over_everything(self):
        """Three-way collision: user > marketplace > builtin."""
        from app.services.skill_routing.namespace import resolve_collision
        cands = [
            {"name": "x", "source": "builtin", "priority": 999},
            {"name": "x", "source": "marketplace", "priority": 999},
            {"name": "x", "source": "user", "priority": 0},
        ]
        result = resolve_collision("x", cands)
        self.assertEqual(result["source"], "user")


class TestSourceTiers(unittest.TestCase):
    """SOURCE_TIERS must assign correct numeric precedence."""

    def test_user_is_lowest(self):
        from app.services.skill_routing.namespace import SOURCE_TIERS
        self.assertLess(SOURCE_TIERS["user"], SOURCE_TIERS["marketplace"])

    def test_marketplace_before_builtin(self):
        from app.services.skill_routing.namespace import SOURCE_TIERS
        self.assertLess(SOURCE_TIERS["marketplace"], SOURCE_TIERS["builtin"])

    def test_generated_before_builtin(self):
        from app.services.skill_routing.namespace import SOURCE_TIERS
        self.assertLess(SOURCE_TIERS["generated"], SOURCE_TIERS["builtin"])

    def test_builtin_equals_bundled(self):
        from app.services.skill_routing.namespace import SOURCE_TIERS
        self.assertEqual(SOURCE_TIERS["builtin"], SOURCE_TIERS["bundled"])


# ==========================================================================
# 2. SkillResolver — priority pipeline
# ==========================================================================


class TestResolverExplicitInvoke(unittest.TestCase):
    """Tier 1: user explicitly picked a skill → use it."""

    def setUp(self):
        from app.services.skill_routing.resolver import SkillResolver
        self.resolver = SkillResolver()

    def test_explicit_picked_skill(self):
        decision = self.resolver.resolve(
            user_message="Make a PPT",
            picked_skill={"name": "my-ppt", "source": "user"},
        )
        self.assertEqual(decision.chosen_skill, "my-ppt")
        self.assertEqual(decision.namespace, "user:my-ppt")
        self.assertEqual(decision.source, "user")
        self.assertFalse(decision.is_default)
        self.assertEqual(decision.reason, "explicit_invoke")

    def test_explicit_with_builtin_source(self):
        decision = self.resolver.resolve(
            user_message="Make a document",
            picked_skill={"name": "docx", "source": "builtin"},
        )
        self.assertEqual(decision.chosen_skill, "docx")
        self.assertEqual(decision.reason, "explicit_invoke")


class TestResolverExclusiveOverride(unittest.TestCase):
    """Tier 1b: picked custom skill with exclusive=True must suppress defaults."""

    def setUp(self):
        from app.services.skill_routing.resolver import SkillResolver
        self.resolver = SkillResolver()

    def test_exclusive_flags_exclusive(self):
        decision = self.resolver.resolve(
            user_message="Make a PPT",
            picked_skill={"name": "my-custom-ppt", "source": "user", "exclusive": True},
        )
        self.assertEqual(decision.chosen_skill, "my-custom-ppt")
        self.assertTrue(decision.exclusive)
        self.assertEqual(decision.reason, "exclusive_override")
        # All defaults bypassed
        self.assertGreater(len(decision.bypassed_defaults), 0)

    def test_exclusive_suppresses_default(self):
        decision = self.resolver.resolve(
            user_message="Make a report",
            picked_skill={"name": "my-custom", "source": "user", "exclusive": True},
        )
        self.assertEqual(decision.reason, "exclusive_override")
        self.assertTrue(decision.exclusive)
        # Even though the message mentions "report" (soft-intent → docx),
        # exclusive override wins.
        self.assertEqual(decision.chosen_skill, "my-custom")

    def test_fallback_allowed_default(self):
        """When not exclusive, fallback is allowed by default."""
        decision = self.resolver.resolve(
            user_message="Make a PPT",
            picked_skill={"name": "my-ppt", "source": "user"},
        )
        self.assertTrue(decision.allow_default_fallback)

    def test_fallback_allowed_explicit_false(self):
        decision = self.resolver.resolve(
            user_message="Make a PPT",
            picked_skill={
                "name": "my-ppt", "source": "user",
                "fallback_allowed": False, "exclusive": False,
            },
        )
        self.assertFalse(decision.allow_default_fallback)


class TestResolverFormatIntent(unittest.TestCase):
    """Tier 3: explicit format detected → auto-pick the default."""

    def setUp(self):
        from app.services.skill_routing.resolver import SkillResolver
        self.resolver = SkillResolver()

    @patch("app.services.skill_routing.resolver.SkillResolver._detect_file_intent")
    def test_pptx_format_routes_to_pptx(self, mock_detect):
        mock_detect.return_value = "pptx"
        decision = self.resolver.resolve(
            user_message="Make a sales report PPT",
            picked_skill=None,
        )
        self.assertEqual(decision.chosen_skill, "ppt-design")
        self.assertTrue(decision.is_default)
        self.assertEqual(decision.reason, "format_intent")
        self.assertEqual(decision.namespace, "builtin:ppt-design")

    @patch("app.services.skill_routing.resolver.SkillResolver._detect_file_intent")
    def test_docx_format_routes_to_docx(self, mock_detect):
        mock_detect.return_value = "docx"
        decision = self.resolver.resolve(
            user_message="Create a Word document",
            picked_skill=None,
        )
        self.assertEqual(decision.chosen_skill, "docx")
        self.assertEqual(decision.reason, "format_intent")

    @patch("app.services.skill_routing.resolver.SkillResolver._detect_file_intent")
    def test_pdf_format_routes_to_pdf(self, mock_detect):
        mock_detect.return_value = "pdf"
        decision = self.resolver.resolve(
            user_message="Generate a PDF report",
            picked_skill=None,
        )
        self.assertEqual(decision.chosen_skill, "pdf")


class TestResolverSoftIntent(unittest.TestCase):
    """Tier 4: no explicit format, but soft-intent heuristic matches."""

    def setUp(self):
        from app.services.skill_routing.resolver import SkillResolver
        self.resolver = SkillResolver()

    @patch("app.services.skill_routing.resolver.SkillResolver._detect_file_intent")
    @patch("app.services.skill_routing.resolver.SkillResolver._detect_soft_intent")
    def test_soft_intent_docx(self, mock_soft, mock_file):
        mock_file.return_value = None          # no explicit format
        mock_soft.return_value = "docx"        # soft intent → docx
        decision = self.resolver.resolve(
            user_message="Make a sales report",
            picked_skill=None,
        )
        self.assertEqual(decision.chosen_skill, "docx")
        self.assertEqual(decision.reason, "soft_intent")
        self.assertTrue(decision.is_default)

    @patch("app.services.skill_routing.resolver.SkillResolver._detect_file_intent")
    @patch("app.services.skill_routing.resolver.SkillResolver._detect_soft_intent")
    def test_format_intent_wins_over_soft_intent(self, mock_soft, mock_file):
        """If both match, format_intent takes priority (checked first)."""
        mock_file.return_value = "pptx"
        mock_soft.return_value = "docx"
        decision = self.resolver.resolve(
            user_message="Make a sales report PPT",
            picked_skill=None,
        )
        self.assertEqual(decision.chosen_skill, "ppt-design")
        self.assertEqual(decision.reason, "format_intent")


class TestResolverFallback(unittest.TestCase):
    """Tier 5: no signal → fallback to docx."""

    def setUp(self):
        from app.services.skill_routing.resolver import SkillResolver
        self.resolver = SkillResolver()

    @patch("app.services.skill_routing.resolver.SkillResolver._detect_file_intent")
    @patch("app.services.skill_routing.resolver.SkillResolver._detect_soft_intent")
    def test_fallback_to_llm_catalog_pick(self, mock_soft, mock_file):
        mock_file.return_value = None
        mock_soft.return_value = None
        decision = self.resolver.resolve(
            user_message="hello",
            picked_skill=None,
        )
        self.assertEqual(decision.chosen_skill, "")
        self.assertEqual(decision.reason, "llm_catalog_pick")
        self.assertFalse(decision.is_default)
        self.assertEqual(decision.namespace, "")


# ==========================================================================
# 3. RoutingDecision dataclass
# ==========================================================================


class TestRoutingDecision(unittest.TestCase):
    def test_default_fields(self):
        from app.services.skill_routing.resolver import RoutingDecision
        rd = RoutingDecision(
            chosen_skill="pptx",
            namespace="builtin:pptx",
            source="builtin",
            is_default=True,
            exclusive=False,
            allow_default_fallback=True,
            reason="format_intent",
        )
        self.assertEqual(rd.bypassed_defaults, [])  # default list

    def test_bypassed_defaults_populated(self):
        from app.services.skill_routing.resolver import RoutingDecision
        rd = RoutingDecision(
            chosen_skill="my-ppt",
            namespace="user:my-ppt",
            source="user",
            is_default=False,
            exclusive=True,
            allow_default_fallback=False,
            reason="exclusive_override",
            bypassed_defaults=["pptx", "docx"],
        )
        self.assertIn("pptx", rd.bypassed_defaults)

    def test_is_dataclass(self):
        from dataclasses import is_dataclass
        from app.services.skill_routing.resolver import RoutingDecision
        self.assertTrue(is_dataclass(RoutingDecision))


# ==========================================================================
# 4. Catalog builder
# ==========================================================================


class TestBuildCatalog(unittest.TestCase):

    def test_empty_skills(self):
        from app.services.skill_routing.catalog import build_catalog
        self.assertEqual(build_catalog([]), "")

    def test_single_skill(self):
        from app.services.skill_routing.catalog import build_catalog
        skills = [{"name": "pptx", "source": "builtin", "description": "Create PPT files"}]
        result = build_catalog(skills)
        self.assertIn("<name>pptx</name>", result)
        self.assertIn("<source>builtin</source>", result)
        self.assertIn("<description>Create PPT files</description>", result)

    def test_skills_sorted_by_priority(self):
        from app.services.skill_routing.catalog import build_catalog
        skills = [
            {"name": "low", "source": "builtin", "priority": 1, "description": "low"},
            {"name": "high", "source": "builtin", "priority": 100, "description": "high"},
        ]
        result = build_catalog(skills)
        idx_high = result.index("high")
        idx_low = result.index("low")
        self.assertLess(idx_high, idx_low, "Higher priority should come first")

    def test_skills_sorted_by_source(self):
        from app.services.skill_routing.catalog import build_catalog
        skills = [
            {"name": "builtin_sk", "source": "builtin", "priority": 0, "description": "b"},
            {"name": "user_sk", "source": "user", "priority": 0, "description": "u"},
        ]
        result = build_catalog(skills)
        idx_user = result.index("user_sk")
        idx_builtin = result.index("builtin_sk")
        self.assertLess(idx_user, idx_builtin, "User source should come before builtin")

    def test_budget_truncation(self):
        from app.services.skill_routing.catalog import build_catalog
        # Create many skills that exceed a tiny budget
        skills = [
            {"name": f"skill_{i}", "source": "builtin", "description": f"Skill number {i}"}
            for i in range(100)
        ]
        budget = 200  # very small, only 1-2 entries fit
        result = build_catalog(skills, budget_chars=budget)
        self.assertTrue(len(result) > 0)
        # Should not contain all 100 entries
        count = result.count("<skill>")
        self.assertLess(count, 100)

    def test_small_budget_does_not_crash(self):
        from app.services.skill_routing.catalog import build_catalog
        # Even a 0-byte budget shouldn't crash
        result = build_catalog(
            [{"name": "pptx", "source": "builtin", "description": "PPT"}],
            budget_chars=0,
        )
        self.assertEqual(result, "")  # nothing fits

    def test_summary_used_when_available(self):
        from app.services.skill_routing.catalog import build_catalog
        skills = [{
            "name": "pptx", "source": "builtin",
            "description": "Create beautiful PowerPoint presentations with charts",
            "summary": "PPT generation",
        }]
        result = build_catalog(skills)
        self.assertIn("PPT generation", result)

    def test_tags_added_to_entry(self):
        from app.services.skill_routing.catalog import build_catalog
        skills = [{
            "name": "pptx", "source": "builtin",
            "description": "Create PPT", "tags": ["ppt", "presentation", "slides"],
        }]
        result = build_catalog(skills)
        self.assertIn("ppt", result)
        self.assertIn("presentation", result)


# ==========================================================================
# 5. Integration: end-to-end routing scenarios
# ==========================================================================


class TestRoutingIntegration(unittest.TestCase):
    """End-to-end routing decisions matching the user's behavioral contract."""

    def setUp(self):
        from app.services.skill_routing.resolver import SkillResolver
        self.resolver = SkillResolver()

    @patch("app.services.skill_routing.resolver.SkillResolver._detect_file_intent")
    def test_generic_ppt_request_routes_to_builtin_pptx(self, mock_detect):
        """'Make a sales report PPT' with no skill picked → built-in pptx."""
        mock_detect.return_value = "pptx"
        decision = self.resolver.resolve(
            user_message="Make a sales report PPT",
            picked_skill=None,
        )
        self.assertEqual(decision.chosen_skill, "ppt-design")
        self.assertTrue(decision.is_default)
        self.assertEqual(decision.source, "builtin")

    def test_custom_skill_bypasses_default(self):
        """Custom skill explicitly invoked → strictly bypass default."""
        decision = self.resolver.resolve(
            user_message="Make a sales report PPT",
            picked_skill={
                "name": "personal-template-ppt",
                "source": "user",
                "exclusive": True,
            },
        )
        self.assertEqual(decision.chosen_skill, "personal-template-ppt")
        self.assertEqual(decision.source, "user")
        self.assertFalse(decision.is_default)
        self.assertEqual(decision.reason, "exclusive_override")
        # The built-in ppt-design (was pptx) must be in bypassed_defaults
        self.assertIn("ppt-design", decision.bypassed_defaults)

    @patch("app.services.skill_routing.resolver.SkillResolver._detect_file_intent")
    def test_custom_fallback_allowed_does_not_bypass_defaults(self, mock_detect):
        """Custom skill with fallback_allowed → no defaults bypassed."""
        mock_detect.return_value = "pptx"
        decision = self.resolver.resolve(
            user_message="Make a PPT",
            picked_skill={"name": "my-ok-ppt", "source": "user", "exclusive": False},
        )
        self.assertEqual(decision.chosen_skill, "my-ok-ppt")
        self.assertTrue(decision.allow_default_fallback)
        self.assertFalse(decision.exclusive)

    @patch("app.services.skill_routing.resolver.SkillResolver._detect_file_intent")
    @patch("app.services.skill_routing.resolver.SkillResolver._detect_soft_intent")
    def test_completely_generic_request_lets_llm_pick(self, mock_soft, mock_file):
        """No format, no soft intent, no picked skill → LLM picks from catalog."""
        mock_file.return_value = None
        mock_soft.return_value = None
        decision = self.resolver.resolve(
            user_message="help me with something",
            picked_skill=None,
        )
        self.assertEqual(decision.chosen_skill, "")
        self.assertEqual(decision.reason, "llm_catalog_pick")

    @patch("app.services.skill_routing.resolver.SkillResolver._detect_file_intent")
    def test_dashboard_request_routes_to_dashboard_generation(self, mock_detect):
        """Explicit dashboard format → live dashboard-generation skill."""
        mock_detect.return_value = "dashboard"
        decision = self.resolver.resolve(
            user_message="Create a dashboard",
            picked_skill=None,
        )
        self.assertEqual(decision.chosen_skill, "dashboard-generation")


# ==========================================================================
# 6. Meta-tool: command parsing
# ==========================================================================


class TestMetaToolActionParsing(unittest.TestCase):

    def test_bare_name_defaults_to_load(self):
        from app.services.skill_routing.meta_tool import _parse_action
        action, name = _parse_action("pptx")
        self.assertEqual(action, "load")
        self.assertEqual(name, "pptx")

    def test_load_prefix(self):
        from app.services.skill_routing.meta_tool import _parse_action
        action, name = _parse_action("load pptx")
        self.assertEqual(action, "load")
        self.assertEqual(name, "pptx")

    def test_execute_prefix(self):
        from app.services.skill_routing.meta_tool import _parse_action
        action, name = _parse_action("execute user:my-ppt")
        self.assertEqual(action, "execute")
        self.assertEqual(name, "user:my-ppt")

    def test_empty_command(self):
        from app.services.skill_routing.meta_tool import _parse_action
        action, name = _parse_action("")
        self.assertEqual(action, "load")
        self.assertEqual(name, "")


# ==========================================================================
# 7. Constraint: no import cycles from public API
# ==========================================================================


class TestPublicAPI(unittest.TestCase):
    """The skill_routing package must import cleanly."""

    def test_import_skill_routing(self):
        import app.services.skill_routing as sr
        self.assertTrue(hasattr(sr, "RoutingDecision"))
        self.assertTrue(hasattr(sr, "SkillResolver"))
        self.assertTrue(hasattr(sr, "build_catalog"))
        self.assertTrue(hasattr(sr, "parse_command"))
        self.assertTrue(hasattr(sr, "resolve_collision"))
        self.assertTrue(hasattr(sr, "to_namespaced"))
        self.assertTrue(hasattr(sr, "SOURCE_TIERS"))
        self.assertTrue(hasattr(sr, "register_skill_meta_tool"))


if __name__ == "__main__":
    unittest.main()
