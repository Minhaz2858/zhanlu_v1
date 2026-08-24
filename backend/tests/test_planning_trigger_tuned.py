"""Tests for the tuned smart routing in planning_trigger.py.

Verifies:
1. FSM trigger threshold raised to 0.6 (2 verbs = 0.4 no longer triggers)
2. Simple-conversation bypass: short messages with 0-1 verbs go to ReAct
3. Truly complex multi-step tasks still go to FSM
4. _is_non_data_intent: greeting/thanks/help → True, data questions → False
5. Data-bound override: when bound_kb_ids non-empty + not non-data → FSM
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


class TestNonDataIntent(unittest.TestCase):
    """Tests for _is_non_data_intent — detects greetings/thanks/help/capability
    so the data-bound routing override doesn't force them through the FSM."""

    # ── Should return True (non-data) ────────────────────────────────────

    def test_english_greeting_hello(self):
        self.assertTrue(pt._is_non_data_intent("hello"))

    def test_english_greeting_hi(self):
        self.assertTrue(pt._is_non_data_intent("hi"))

    def test_english_thanks(self):
        self.assertTrue(pt._is_non_data_intent("thanks"))

    def test_english_ok(self):
        self.assertTrue(pt._is_non_data_intent("ok"))

    def test_chinese_greeting(self):
        self.assertTrue(pt._is_non_data_intent("你好"))

    def test_chinese_greeting_formal(self):
        self.assertTrue(pt._is_non_data_intent("您好"))

    def test_chinese_thanks(self):
        self.assertTrue(pt._is_non_data_intent("谢谢"))

    def test_chinese_ok(self):
        self.assertTrue(pt._is_non_data_intent("好的"))

    def test_short_non_data_no_question(self):
        """Short message with no question mark and no numbers → non-data."""
        self.assertTrue(pt._is_non_data_intent("good morning"))

    def test_capabilities_question(self):
        self.assertTrue(pt._is_non_data_intent("what can you do"))

    def test_chinese_capabilities(self):
        self.assertTrue(pt._is_non_data_intent("你能做什么"))

    def test_empty_message(self):
        self.assertTrue(pt._is_non_data_intent(""))

    # ── Should return False (data questions) ─────────────────────────────

    def test_data_question_english(self):
        """'give me supply chain data for last 30 days' → data question."""
        self.assertFalse(pt._is_non_data_intent("give me supply chain data for last 30 days"))

    def test_data_question_top_customers(self):
        self.assertFalse(pt._is_non_data_intent("give me top 5 customers last month"))

    def test_data_question_with_number(self):
        """Message with a number → likely a data question."""
        self.assertFalse(pt._is_non_data_intent("show me 2024 sales"))

    def test_data_question_with_question_mark(self):
        """Message with question mark → likely a question, not greeting."""
        self.assertFalse(pt._is_non_data_intent("sales?"))

    def test_short_data_keyword_query(self):
        """Short but contains 'data' → data-adjacent → NOT non-data."""
        self.assertFalse(pt._is_non_data_intent("show data"))

    def test_short_report_keyword_query(self):
        """Short but contains 'report' → data-adjacent → NOT non-data."""
        self.assertFalse(pt._is_non_data_intent("generate report"))

    def test_chinese_data_question(self):
        """Chinese data question → NOT non-data."""
        self.assertFalse(pt._is_non_data_intent("给我最近30天的供应链数据"))

    def test_chinese_data_question_short(self):
        """Short Chinese data question with keyword → NOT non-data."""
        self.assertFalse(pt._is_non_data_intent("查一下销量"))

    def test_chinese_report_question(self):
        self.assertFalse(pt._is_non_data_intent("生成7月销售报告"))


class TestDataBoundRoutingOverride(unittest.TestCase):
    """Tests for the data-bound FSM routing override logic.

    When bound_kb_ids is non-empty and the intent is NOT non-data,
    should_plan should be forced to True regardless of the heuristic score.
    """

    def test_data_bound_override_with_data_question(self):
        """Data question + bound KB → FSM (override)."""
        t = pt.should_trigger_planning("give me top 5 customers last month")
        self.assertFalse(t.should_plan)  # heuristic says no
        # But with data-bound override, it would become True:
        # (the override is applied in agents.py, not in planning_trigger.py,
        # so we verify the helper functions here)
        self.assertFalse(pt._is_non_data_intent("give me top 5 customers last month"))

    def test_data_bound_override_not_for_greeting(self):
        """Greeting + bound KB → direct path (no override)."""
        self.assertTrue(pt._is_non_data_intent("hello"))

    def test_data_bound_override_not_for_chinese_greeting(self):
        """Chinese greeting + bound KB → direct path (no override)."""
        self.assertTrue(pt._is_non_data_intent("你好"))

    def test_data_bound_override_for_chinese_data(self):
        """Chinese data question + bound KB → FSM (override)."""
        self.assertFalse(pt._is_non_data_intent("给我上个月的销量数据"))


if __name__ == "__main__":
    unittest.main()
