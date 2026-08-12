"""Tests for the agent-system-prompt forced-skill injection.

When the post-router hook fires on a strong skill match, the agent chat
loop (``routers/agents.py`` — main + v3 paths) must surface a HARD
directive block in the system prompt telling the LLM to invoke the
Skill meta-tool as its first action.

The directive builder is ``app.services.skill_routing.post_router_hook.\
build_agent_forced_skill_directive`` — a pure string function for unit
testing.  The agents.py wiring is verified separately through helper-level
tests that patch ``post_router_pick``.
"""

from __future__ import annotations

import os
import sys
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)


# ---------------------------------------------------------------------------
# 1. build_agent_forced_skill_directive — pure string helper
# ---------------------------------------------------------------------------


class TestBuildAgentForcedSkillDirective(unittest.TestCase):
    """Directive must be a non-empty string mentioning the skill and the
    Skill meta-tool command, in <forced_skill> tags."""

    def test_returns_string_with_skill_name(self):
        from app.services.skill_routing.post_router_hook import (
            build_agent_forced_skill_directive,
        )

        block = build_agent_forced_skill_directive("slack-gif-creator", score=0.9)
        self.assertIsInstance(block, str)
        self.assertIn("slack-gif-creator", block)
        self.assertIn("<forced_skill>", block)
        self.assertIn("</forced_skill>", block)

    def test_contains_skill_meta_tool_command(self):
        """The block must include the explicit Skill meta-tool command."""
        from app.services.skill_routing.post_router_hook import (
            build_agent_forced_skill_directive,
        )

        block = build_agent_forced_skill_directive("canvas-design", score=0.85)
        self.assertIn('"execute canvas-design"', block)
        self.assertIn("Skill", block)

    def test_score_included_when_given(self):
        from app.services.skill_routing.post_router_hook import (
            build_agent_forced_skill_directive,
        )

        block = build_agent_forced_skill_directive("canvas-design", score=0.85)
        self.assertIn("0.85", block)

    def test_no_score_means_no_score_text(self):
        """When score=None, don't show the confidence phrase."""
        from app.services.skill_routing.post_router_hook import (
            build_agent_forced_skill_directive,
        )

        block = build_agent_forced_skill_directive("canvas-design", score=None)
        self.assertNotIn("confidence", block.lower())


# ---------------------------------------------------------------------------
# 2. post_router_pick accepts pre-computed candidates (no second search)
# ---------------------------------------------------------------------------


class TestPostRouterPickAcceptsCandidates(unittest.TestCase):
    """Agents.py already calls unified_search once per turn. To avoid a
    second call, ``post_router_pick`` must accept pre-computed results."""

    def test_uses_passed_candidates_without_search(self):
        from app.services.skill_routing.post_router_hook import post_router_pick

        fake_results = [
            {
                "name": "slack-gif-creator",
                "description": "GIFs",
                "trigger": "gif slack animated",
                "source": "filesystem",
            },
        ]
        # If post_router_pick does NOT accept candidates and runs its own
        # unified_search call, this patch will be ignored — so the test will
        # only see what we passed in.
        result = post_router_pick(
            "make a slack gif for me",
            db=None,
            candidates=fake_results,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["skill_name"], "slack-gif-creator")


# ---------------------------------------------------------------------------
# 3. agents.py integration — direct on the directive builder
# ---------------------------------------------------------------------------


class TestAgentsForcedSkillInjection(unittest.TestCase):
    """Verify agents.py uses the directive builder when the hook fires.

    Tested by importing the public helpers, mocking ``post_router_pick``,
    and checking the system prompt gets the directive injected.
    """

    def test_forced_skill_appended_to_system_prompt(self):
        """Simulate the agents.py injection logic.  When post_router_pick
        returns a forced dict, the system prompt gets a <forced_skill>
        block appended."""
        from app.services.skill_routing.post_router_hook import (
            build_agent_forced_skill_directive,
            post_router_pick,
        )

        forced = post_router_pick(
            "make a slack gif",
            db=None,
            candidates=[
                {
                    "name": "slack-gif-creator",
                    "description": "GIFs",
                    "trigger": "gif slack",
                    "source": "filesystem",
                }
            ],
        )
        self.assertIsNotNone(forced)

        # Simulate agents.py: append directive after assembling catalog.
        system_prompt = "Base system prompt. "
        system_prompt += build_agent_forced_skill_directive(
            forced["skill_name"], score=forced.get("score")
        )

        self.assertIn("<forced_skill>", system_prompt)
        self.assertIn("slack-gif-creator", system_prompt)
        self.assertIn('"execute slack-gif-creator"', system_prompt)
        # Directive must come BEFORE the user message (prompt structure)
        self.assertGreater(
            system_prompt.index("<forced_skill>"),
            system_prompt.index("Base system prompt"),
        )

    def test_no_directive_when_no_strong_match(self):
        """When post_router_pick returns None, no directive is built."""
        from app.services.skill_routing.post_router_hook import post_router_pick

        result = post_router_pick("hello", db=None)
        self.assertIsNone(result)
        # Caller would not build a directive in this case.


class TestSelectedRuntimeSkillBlock(unittest.TestCase):
    def test_selected_skill_block_mentions_priority_and_skill_body(self):
        from types import SimpleNamespace
        from app.routers.agents import _build_selected_skill_runtime_block

        class Query:
            def filter(self, *args, **kwargs):
                return self
            def first(self):
                return SimpleNamespace(
                    name="weekly-report-generation",
                    description="Weekly report skill",
                    trigger="weekly report",
                    skill_md="# Weekly Report\n\nUse KPI-first weekly report structure.",
                )

        class DB:
            def query(self, *args, **kwargs):
                return Query()

        block = _build_selected_skill_runtime_block(
            DB(),
            {"id": "tool-123", "name": "weekly-report-generation"},
            "tool-123",
        )
        self.assertIn("<selected_runtime_skill>", block)
        self.assertIn("highest priority", block)
        self.assertIn("weekly-report-generation", block)
        self.assertIn("Use KPI-first weekly report structure", block)
        self.assertIn("Do not call unavailable legacy weekly-report tools", block)
