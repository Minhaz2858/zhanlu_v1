"""Tests for the shared INITIATIVE block and FSM latitude policy.

Claude's pattern from the reference screenshots: when the user grants
open latitude ("any data you can use", "use fake data"), the agent
proceeds with clearly-marked synthetic defaults instead of asking a
questionnaire. When a choice genuinely matters, it offers ≤3 numbered
options with a recommendation.
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)


class TestInitiativeBlock(unittest.TestCase):
    def test_block_exists_and_has_content(self):
        from app.services.agent_prompts import _INITIATIVE_BLOCK
        self.assertIsInstance(_INITIATIVE_BLOCK, str)
        self.assertGreater(len(_INITIATIVE_BLOCK), 100)

    def test_block_covers_open_latitude(self):
        from app.services.agent_prompts import _INITIATIVE_BLOCK
        lower = _INITIATIVE_BLOCK.lower()
        # Latitude phrases the block must recognize.
        self.assertIn("any data", lower)
        self.assertIn("you choose", lower)
        # The core rule: don't ask, proceed.
        self.assertIn("do not ask", lower)
        self.assertIn("proceed", lower)

    def test_block_requires_marking_demo_data(self):
        from app.services.agent_prompts import _INITIATIVE_BLOCK
        lower = _INITIATIVE_BLOCK.lower()
        self.assertIn("indicative", lower)  # clearly-marked synthetic data
        self.assertIn("never present invented numbers as real", lower)

    def test_block_caps_clarification_at_numbered_options(self):
        from app.services.agent_prompts import _INITIATIVE_BLOCK
        lower = _INITIATIVE_BLOCK.lower()
        self.assertIn("3 numbered options", lower)
        self.assertIn("recommendation", lower)
        self.assertIn("never a questionnaire", lower)


class TestInitiativeBlockAppendedToAllPaths(unittest.TestCase):
    """get_system_prompt must append the initiative block on every path."""

    def test_general_assistant_has_initiative_block(self):
        from app.services.agent_prompts import get_system_prompt, _INITIATIVE_BLOCK
        self.assertIn(_INITIATIVE_BLOCK, get_system_prompt("general_assistant"))

    def test_power_user_has_initiative_block(self):
        from app.services.agent_prompts import get_system_prompt, _INITIATIVE_BLOCK
        self.assertIn(_INITIATIVE_BLOCK, get_system_prompt("power_user"))

    def test_generic_fallback_has_initiative_block(self):
        from app.services.agent_prompts import get_system_prompt, _INITIATIVE_BLOCK
        self.assertIn(_INITIATIVE_BLOCK, get_system_prompt("totally_unknown_agent"))

    def test_tone_block_still_present(self):
        """Regression: adding the initiative block must not drop the tone block."""
        from app.services.agent_prompts import (
            get_system_prompt, _CONVERSATION_TONE_BLOCK,
        )
        self.assertIn(_CONVERSATION_TONE_BLOCK, get_system_prompt("general_assistant"))


class TestFsmLatitudePolicy(unittest.TestCase):
    def _build_prompt(self, message: str) -> str:
        from app.services.synexia.fsm import SynexiaFSM, ExecutionRequest
        fsm = SynexiaFSM.__new__(SynexiaFSM)  # no DB needed for prompt building
        # _build_response_prompt reads these three execution attrs.
        fsm.execution = SimpleNamespace(
            observations=None, task_spec=None, context_manifest=None,
        )
        req = ExecutionRequest(
            conversation_id="c1",
            agent_name="general_assistant",
            user_message=message,
        )
        return fsm._build_response_prompt(req)

    def test_policy_mentions_open_latitude(self):
        prompt = self._build_prompt("make a sales report, any data you can use")
        lower = prompt.lower()
        self.assertIn("open latitude", lower)
        self.assertIn("any data", lower)

    def test_policy_requires_marked_demo_data(self):
        prompt = self._build_prompt("make a sales report, any data you can use")
        lower = prompt.lower()
        self.assertIn("indicative", lower)

    def test_policy_caps_options(self):
        prompt = self._build_prompt("build me something")
        lower = prompt.lower()
        self.assertIn("3 numbered options", lower)
        self.assertIn("never a questionnaire", lower)

    def test_one_question_rule_preserved(self):
        """Regression: the original at-most-one-question rule must remain."""
        prompt = self._build_prompt("make a deck")
        self.assertIn("at most ONE clarifying question", prompt)
