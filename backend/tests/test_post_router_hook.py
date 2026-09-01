"""Tests for the post-router skill auto-selection hook.

Covers:

* ``post_router_hook.score_skill_match`` — 0–1 scoring of user messages
  against skill metadata, rewarding exact skill-name tokens and trigger
  keyword hits.
* ``post_router_hook.post_router_pick`` — top-level hook that runs
  ``unified_search`` and returns a forced-skill dict when the top result
  crosses ``STRONG_MATCH_THRESHOLD``.  Returns ``None`` for greetings,
  empty messages, and weak matches.
* Integration: ``pick_default_skill`` (in ``default_skills``) surfaces the
  forced skill to the TaskSpec when the hook identifies a strong match.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)


# ---------------------------------------------------------------------------
# 1. score_skill_match — pure scoring function
# ---------------------------------------------------------------------------


class TestScoreSkillMatch(unittest.TestCase):
    """score_skill_match must return a float in [0.0, 1.0]."""

    def test_returns_float_in_range(self):
        """A normal input returns a float clamped to [0, 1]."""
        from app.services.skill_routing.post_router_hook import score_skill_match

        skill = {"name": "slack-gif-creator", "description": "Make Slack GIFs", "trigger": "gif"}
        score = score_skill_match("make a slack gif", skill)
        self.assertIsInstance(score, float)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_empty_message_returns_zero(self):
        """Empty / whitespace messages must yield 0.0 — no scoring possible."""
        from app.services.skill_routing.post_router_hook import score_skill_match

        skill = {"name": "docx", "description": "Word docs", "trigger": "docx"}
        self.assertEqual(score_skill_match("", skill), 0.0)
        self.assertEqual(score_skill_match("   ", skill), 0.0)
        self.assertEqual(score_skill_match(None, skill), 0.0)

    def test_exact_skill_name_in_message_high_score(self):
        """When the full skill name appears, the score reaches >= 0.6."""
        from app.services.skill_routing.post_router_hook import (
            STRONG_MATCH_THRESHOLD,
            score_skill_match,
        )

        skill = {"name": "slack-gif-creator", "description": "x", "trigger": "gif"}
        score = score_skill_match("can you run slack-gif-creator for me", skill)
        self.assertGreaterEqual(
            score,
            STRONG_MATCH_THRESHOLD,
            f"expected score >= {STRONG_MATCH_THRESHOLD}, got {score}",
        )

    def test_multiple_name_tokens_score_above_threshold(self):
        """Multi-token skill name with each token present in the message
        must score >= 0.6."""
        from app.services.skill_routing.post_router_hook import (
            STRONG_MATCH_THRESHOLD,
            score_skill_match,
        )

        skill = {"name": "canvas-design", "description": "x", "trigger": "design"}
        score = score_skill_match("please help me with canvas design mockups", skill)
        self.assertGreaterEqual(score, STRONG_MATCH_THRESHOLD)

    def test_no_token_overlap_returns_zero(self):
        """No tokens overlap → score 0.0."""
        from app.services.skill_routing.post_router_hook import score_skill_match

        skill = {"name": "slack-gif-creator", "description": "x", "trigger": "gif"}
        score = score_skill_match("what is the meaning of life", skill)
        self.assertEqual(score, 0.0)

    def test_short_message_with_single_generic_token_below_threshold(self):
        """A single short match with no other tokens must not force."""
        from app.services.skill_routing.post_router_hook import (
            STRONG_MATCH_THRESHOLD,
            score_skill_match,
        )

        # "doc" is a 3-letter token but generic; should not force.
        skill = {"name": "docx", "description": "Word docs", "trigger": "docx doc"}
        score = score_skill_match("doc", skill)
        self.assertLess(score, STRONG_MATCH_THRESHOLD)


# ---------------------------------------------------------------------------
# 2. post_router_pick — orchestration (stopword + unified_search + scoring)
# ---------------------------------------------------------------------------


class TestPostRouterPick(unittest.TestCase):
    """post_router_pick orchestrates stopword guard, unified_search, and scoring."""

    def test_returns_none_for_empty_message(self):
        """Empty messages skip unified_search entirely and return None."""
        from app.services.skill_routing.post_router_hook import post_router_pick

        self.assertIsNone(post_router_pick(""))
        self.assertIsNone(post_router_pick(None))

    def test_returns_none_for_stopword_only_message(self):
        """Greetings / small-talk must not trigger any skill."""
        from app.services.skill_routing.post_router_hook import post_router_pick

        for msg in ["hello", "hi there", "thanks!", "ok", "yes please", "lol"]:
            self.assertIsNone(
                post_router_pick(msg),
                f"expected None for greeting: {msg!r}",
            )

    def test_returns_none_when_unified_search_empty(self):
        """If no candidate skills match the message, return None."""
        from app.services.skill_routing.post_router_hook import post_router_pick

        with patch(
            "app.services.skill_routing.post_router_hook.unified_search",
            return_value=[],
        ):
            self.assertIsNone(post_router_pick("explain kubernetes to me"))

    def test_returns_none_when_top_score_below_threshold(self):
        """If the best match scores below STRONG_MATCH_THRESHOLD, return None."""
        from app.services.skill_routing.post_router_hook import post_router_pick

        # Weak match: 'doc' is a generic 3-letter token; message has only that token.
        fake_results = [
            {"name": "docx", "description": "Word", "trigger": "docx", "source": "filesystem"},
        ]
        with patch(
            "app.services.skill_routing.post_router_hook.unified_search",
            return_value=fake_results,
        ):
            self.assertIsNone(post_router_pick("doc"))

    def test_returns_forced_dict_when_strong_match(self):
        """A strong match returns a dict with 'skill_name' and 'forced': True."""
        from app.services.skill_routing.post_router_hook import post_router_pick

        fake_results = [
            {
                "name": "slack-gif-creator",
                "description": "Create animated GIFs for Slack",
                "trigger": "gif slack animated",
                "source": "filesystem",
            },
        ]
        with patch(
            "app.services.skill_routing.post_router_hook.unified_search",
            return_value=fake_results,
        ):
            result = post_router_pick("make a slack gif for me")
            self.assertIsNotNone(result)
            self.assertIsInstance(result, dict)
            self.assertEqual(result["skill_name"], "slack-gif-creator")
            self.assertTrue(result["forced"])
            self.assertIn("score", result)
            self.assertGreaterEqual(result["score"], 0.6)

    def test_dashboard_intent_prefers_dashboard_generation_over_uiux_companion(self):
        """Dashboard requests may mention UI UX Pro Max, but the owning
        skill must remain dashboard-generation; ui-ux-pro-max is a companion
        tool/skill used inside that workflow, not the turn owner."""
        from app.services.skill_routing.post_router_hook import post_router_pick

        result = post_router_pick(
            "Build a live sales dashboard. Use UI UX Pro Max design guidance.",
            candidates=[
                {
                    "name": "ui-ux-pro-max",
                    "description": "Design intelligence",
                    "trigger": "ui ux design dashboard chart",
                    "source": "filesystem",
                },
                {
                    "name": "dashboard-generation",
                    "description": "Build live database dashboards",
                    "trigger": "dashboard kpi metrics chart",
                    "source": "filesystem",
                },
            ],
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["skill_name"], "dashboard-generation")
        self.assertTrue(result["forced"])

    def test_dashboard_intent_prefers_dashboard_generation_even_when_message_negates_html(self):
        from app.services.skill_routing.post_router_hook import post_router_pick

        result = post_router_pick(
            "Build a live sales dashboard with UI UX design guidance. Use create_dashboard, not HTML.",
            candidates=[
                {"name": "ui-ux-pro-max", "trigger": "ui ux design dashboard chart", "source": "filesystem"},
                {"name": "dashboard-generation", "trigger": "dashboard kpi metrics chart", "source": "filesystem"},
            ],
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["skill_name"], "dashboard-generation")



# ---------------------------------------------------------------------------
# 4. Module exports
# ---------------------------------------------------------------------------


class TestModuleExports(unittest.TestCase):
    """The public API must be re-exported from the skill_routing package."""

    def test_exports_in_package_init(self):
        from app.services.skill_routing import (
            post_router_pick,
            score_skill_match,
            STRONG_MATCH_THRESHOLD,
        )

        assert callable(post_router_pick)
        assert callable(score_skill_match)
        assert isinstance(STRONG_MATCH_THRESHOLD, float)

# Note: integration with ``default_skills.pick_default_skill`` is tested
# separately under ``tests/test_post_router_pick_integration.py`` once the
# wiring (Todo #2) is in place.
