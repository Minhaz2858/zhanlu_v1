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

    def test_ambiguous_request_no_longer_returns_forced_skill(self):
        """Phase A/B: pick_default_skill no longer consults the post-router.
        An ambiguous request (no explicit format word) returns None and the
        LLM picks from the full skill catalog."""
        from app.services.synexia.default_skills import pick_default_skill

        result = pick_default_skill("make a slack gif for me")
        self.assertIsNone(result)

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

    def test_generic_message_leaves_no_forced_skill(self):
        """Without a deterministic forced skill, TaskSpec carries no
        forced_skill — the planner lets the LLM choose."""
        from app.services.synexia import task_spec_parser

        task_spec = task_spec_parser.parse_task_spec(
            user_message="explain something",
            agent_name="general_assistant",
            conversation_context=None,
        )

        self.assertIsNotNone(task_spec)
        self.assertFalse(task_spec.get("forced_skill"))

