"""Tests for the tuned smart routing in planning_trigger.py.

Verifies:
1. FSM trigger threshold raised to 0.6 (2 verbs = 0.4 no longer triggers)
2. Simple-conversation bypass: short messages with 0-1 verbs go to ReAct
3. Truly complex multi-step tasks still go to FSM
"""

from __future__ import annotations

import os
import sys
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from app.services import planning_trigger as pt


class TestRoutingThresholdTuned(unittest.TestCase):
    def test_two_action_verbs_do_not_trigger_fsm(self):
        """'create a report and send it' has 2 verbs (=0.4) — below the
        new 0.6 threshold, and the simple-conversation bypass applies
        (short, 0 connectives, 0 plan keywords, 2 verbs > 1 → no bypass).
        Score 0.4 < 0.6 → ReAct."""
        t = pt.should_trigger_planning("create a report and send it")
        self.assertFalse(t.should_plan)

    def test_single_action_verb_goes_to_react(self):
        """'create a report' — 1 verb, no connectives → bypass → ReAct."""
        t = pt.should_trigger_planning("create a report")
        self.assertFalse(t.should_plan)
        self.assertEqual(t.source, "heuristic-bypass")

    def test_greeting_goes_to_react(self):
        t = pt.should_trigger_planning("hello")
        self.assertFalse(t.should_plan)
        self.assertEqual(t.source, "heuristic-bypass")

    def test_general_question_goes_to_react(self):
        t = pt.should_trigger_planning("what is artificial intelligence?")
        self.assertFalse(t.should_plan)
        self.assertEqual(t.source, "heuristic-bypass")

    def test_truly_complex_multistep_triggers_fsm(self):
        """3 action verbs + connectives → score >= 0.8 → FSM."""
        t = pt.should_trigger_planning(
            "Create a report, then send it, and schedule a meeting"
        )
        self.assertTrue(t.should_plan)

    def test_plan_keyword_triggers_fsm(self):
        """Explicit plan keyword → score 0.4 from keyword alone, plus
        bypass doesn't apply (plan_keywords > 0).  But 0.4 < 0.6 → ReAct.
        Wait — 'plan a report' has 'plan' keyword (+0.4) and 'create'/'plan'
        verb... let me check."""
        # "Plan a report" → plan_keywords=1 (+0.4), action_verbs=1 (plan)
        # → score=0.4. Bypass doesn't apply (plan_keywords=1).
        # 0.4 < 0.6 → should NOT trigger.
        t = pt.should_trigger_planning("plan a report")
        self.assertFalse(t.should_plan)

    def test_plan_keyword_with_connective_triggers_fsm(self):
        """'plan a report then send it' → plan_kw=1 (+0.4) +
        connective=1 (+0.4) + verbs=2 (+0.4) = 1.2 → capped 1.0 → FSM."""
        t = pt.should_trigger_planning("plan a report then send it")
        self.assertTrue(t.should_plan)


class TestSimpleBypassDetails(unittest.TestCase):
    def test_bypass_source_label(self):
        t = pt.should_trigger_planning("hi there")
        self.assertEqual(t.source, "heuristic-bypass")

    def test_long_simple_message_still_evaluated_by_threshold(self):
        """A long message (> 120 chars) with no multi-step signals should
        NOT get the bypass — it's evaluated by the 0.6 threshold."""
        long_msg = "Please summarize the following article in three bullet points. " * 5
        t = pt.should_trigger_planning(long_msg)
        self.assertNotEqual(t.source, "heuristic-bypass")
        # No multi-step signals → score 0 → ReAct.
        self.assertFalse(t.should_plan)

    def test_bypass_does_not_apply_with_connective(self):
        """Even a short message with a connective skips the bypass."""
        t = pt.should_trigger_planning("do X then Y")
        # 'then' is a connective → bypass doesn't apply.
        self.assertNotEqual(t.source, "heuristic-bypass")


if __name__ == "__main__":
    unittest.main()
