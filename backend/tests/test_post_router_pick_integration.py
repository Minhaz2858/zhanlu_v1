"""Integration tests: post-router hook wired into ``pick_default_skill``.

Covers the wiring activated by the post-router-skill-auto-selection-hook plan:

* ``pick_default_skill`` calls ``post_router_pick`` at step 4 (the
  ``llm_catalog_pick`` fallback).
* When a strong match is found, ``pick_default_skill`` returns the
  forced-skill dict with ``"forced": True``.
* When no strong match, behavior is unchanged (returns ``None``).
* ``task_spec_parser`` propagates the forced-skill flag onto the
  TaskSpec (``forced_skill`` / ``forced_skill_score``) for downstream
  consumers (``plan_dag``, agent system prompt).
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
# 1. pick_default_skill integration
# ---------------------------------------------------------------------------


class TestPickDefaultSkillHook(unittest.TestCase):
    """``pick_default_skill`` must consult the post-router hook at step 4."""

    def test_returns_none_for_greeting_unchanged(self):
        """Greetings must short-circuit before the hook fires (existing behavior)."""
        from app.services.synexia.default_skills import pick_default_skill

        self.assertIsNone(pick_default_skill("hello there"))
        self.assertIsNone(pick_default_skill("thanks!"))

    def test_returns_none_when_no_strong_match(self):
        """Messages with no strong skill match return ``None`` like before."""
        from app.services.synexia.default_skills import pick_default_skill

        with patch(
            "app.services.skill_routing.post_router_hook.unified_search",
            return_value=[],
        ):
            self.assertIsNone(pick_default_skill("explain kubernetes to me"))

    def test_returns_forced_dict_on_strong_match(self):
        """When ``post_router_pick`` finds a strong match, the dict returned
        by ``pick_default_skill`` carries ``forced: True`` so downstream
        code can build a HARD directive (not just a soft hint)."""
        from app.services.synexia.default_skills import pick_default_skill

        fake_results = [
            {
                "name": "slack-gif-creator",
                "description": "Make animated GIFs for Slack",
                "trigger": "gif slack animated",
                "source": "filesystem",
            },
        ]
        with patch(
            "app.services.skill_routing.post_router_hook.unified_search",
            return_value=fake_results,
        ):
            result = pick_default_skill("make a slack gif for me")
            self.assertIsNotNone(result)
            self.assertEqual(result["skill_name"], "slack-gif-creator")
            self.assertTrue(result["forced"])
            self.assertGreaterEqual(result["score"], 0.6)

    def test_explicit_format_intent_unaffected(self):
        """An explicit .docx keyword must still take format-intent priority
        over the post-router hook. Format-intent (Path A) is untouched."""
        from app.services.synexia.default_skills import pick_default_skill

        with patch(
            "app.services.skill_routing.post_router_hook.unified_search",
        ) as mock_search:
            mock_search.return_value = [
                {
                    "name": "slack-gif-creator",
                    "description": "GIFs",
                    "trigger": "gif slack",
                    "source": "filesystem",
                }
            ]
            result = pick_default_skill("please write me a .docx file")
            self.assertIsNotNone(result)
            self.assertEqual(result["skill_name"], "docx")
            # Post-router hook should NOT be consulted when format intent matches
            mock_search.assert_not_called()


# ---------------------------------------------------------------------------
# 2. task_spec_parser TaskSpec propagation
# ---------------------------------------------------------------------------


class TestForcedSkillInTaskSpec(unittest.TestCase):
    """``task_spec_parser`` must surface ``forced_skill`` on the TaskSpec
    whenever the post-router hook fired (so plan_dag / agents.py can build
    a hard directive)."""

    def test_forced_skill_appears_on_task_spec(self):
        """When pick_default_skill returns a forced dict, ``task_spec_parser``
        sets ``task_spec["forced_skill"]`` (and ``forced_skill_score``)."""
        from app.services.synexia import task_spec_parser

        forced_dict = {
            "skill_name": "canvas-design",
            "triggers": [],
            "format": None,
            "forced": True,
            "score": 0.85,
            "source": "filesystem",
        }

        # Patch the post-router hook at its consumer location so the call
        # inside pick_default_skill returns our forced dict.
        with patch(
            "app.services.synexia.default_skills.post_router_pick",
            return_value=forced_dict,
        ):
            task_spec = task_spec_parser.parse_task_spec(
                user_message="help me with canvas design mockups",
                agent_name="general_assistant",
                conversation_context=None,
            )

        self.assertIsNotNone(task_spec)
        self.assertTrue(task_spec.get("forced_skill"))
        self.assertEqual(task_spec.get("forced_skill_name"), "canvas-design")
        self.assertGreaterEqual(task_spec.get("forced_skill_score", 0), 0.6)

    def test_no_forced_skill_when_no_strong_match(self):
        """Generic messages leave ``forced_skill`` falsy on the TaskSpec."""
        from app.services.synexia import task_spec_parser

        with patch(
            "app.services.synexia.default_skills.post_router_pick",
            return_value=None,
        ):
            task_spec = task_spec_parser.parse_task_spec(
                user_message="explain something",
                agent_name="general_assistant",
                conversation_context=None,
            )

        self.assertIsNotNone(task_spec)
        self.assertFalse(task_spec.get("forced_skill"))
