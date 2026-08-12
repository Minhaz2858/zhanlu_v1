"""Tests for the ``forced_skill`` directive in ``plan_dag``.

When the post-router hook fires on a strong skill match, the FSM planner
path must NOT just suggest a skill step (soft hint). Instead it must emit a
*hard* directive in the planner prompt AND prepend a ``node_type: "skill"``
node to the fallback plan emitted by ``_build_default_plan``.
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
# 1. build_default_plan_prompt: hard directive when forced_skill is set
# ---------------------------------------------------------------------------


class TestHardDirectiveInPlannerPrompt(unittest.TestCase):
    """The planner prompt extra must contain a HARD directive when the
    TaskSpec carries a forced skill (carried from the post-router hook)."""

    def _call(self, task_spec: dict) -> str:
        """Invoke the helper that builds the skill directive block."""
        from app.services.synexia import plan_dag

        return plan_dag._build_skill_directive_block(task_spec) or ""

    def test_hard_directive_present_when_forced(self):
        """When forced_skill is set, the helper must return a hard directive
        naming the forced skill."""
        task_spec = {
            "user_message": "make a slack gif for me",
            "forced_skill": True,
            "forced_skill_name": "slack-gif-creator",
            "forced_skill_score": 0.95,
        }
        prompt = self._call(task_spec)
        self.assertIn("slack-gif-creator", prompt)
        lowered = prompt.lower()
        self.assertTrue(
            "must" in lowered or "required" in lowered or "directive" in lowered,
            f"expected hard-directive language; got: {prompt!r}",
        )

    def test_empty_directive_when_not_forced(self):
        """When forced_skill is False, the helper returns an empty string."""
        task_spec = {
            "user_message": "explain kubernetes",
            "forced_skill": False,
        }
        prompt = self._call(task_spec)
        self.assertEqual(prompt, "")


# ---------------------------------------------------------------------------
# 2. _build_default_plan: prepend a node_type=skill step when forced
# ---------------------------------------------------------------------------


class TestForcedSkillInDefaultPlan(unittest.TestCase):
    """``_build_default_plan`` must prepend a skill node when forced_skill."""

    def test_prepends_skill_node_when_forced(self):
        from app.services.synexia.plan_dag import _build_default_plan

        task_spec = {
            "user_message": "make a slack gif for me",
            "forced_skill": True,
            "forced_skill_name": "slack-gif-creator",
            "forced_skill_score": 0.95,
            "task_kind": "general",
            "user_signal": "default",
            "requires_data": False,
            "artifact_intents": [],
        }
        steps = _build_default_plan(task_spec, "general_assistant")
        self.assertIsInstance(steps, list)
        self.assertGreater(len(steps), 0)
        first = steps[0]
        self.assertEqual(first["node_type"], "skill")
        self.assertEqual(first["skill"], "slack-gif-creator")
        # Subsequent steps must depend on the skill node (index 0)
        if len(steps) > 1:
            self.assertEqual(steps[1]["dependencies"], [0])

    def test_no_skill_node_when_not_forced(self):
        from app.services.synexia.plan_dag import _build_default_plan

        task_spec = {
            "user_message": "explain something",
            "forced_skill": False,
            "task_kind": "general",
            "user_signal": "default",
            "requires_data": False,
            "artifact_intents": [],
        }
        steps = _build_default_plan(task_spec, "general_assistant")
        self.assertIsInstance(steps, list)
        for step in steps:
            self.assertNotEqual(step["node_type"], "skill")

    def test_selected_skill_precedes_forced_skill(self):
        from app.services.synexia.plan_dag import _build_default_plan

        task_spec = {
            "user_message": "make a weekly sales report",
            "selected_skill": {"id": "tool-123", "name": "weekly-sales-report"},
            "selected_skill_id": "tool-123",
            "selected_skill_name": "weekly-sales-report",
            "forced_skill": True,
            "forced_skill_name": "docx",
            "task_kind": "create_artifact",
            "user_signal": "export_pptx",
            "requires_data": True,
            "artifact_intents": ["pptx"],
        }
        steps = _build_default_plan(task_spec, "general_assistant")
        self.assertGreater(len(steps), 0)
        first = steps[0]
        self.assertEqual(first["node_type"], "skill")
        self.assertEqual(first["inputs"]["skill_id"], "tool-123")
        self.assertEqual(first["inputs"]["skill_name"], "weekly-sales-report")
        self.assertEqual(first["skill"], "weekly-sales-report")
