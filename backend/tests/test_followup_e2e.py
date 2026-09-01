"""Multi-turn follow-up regression test — reproduces the exact failing
scenario from the user's screenshots:

  Turn 1: "make sales report fro me and give me in ppt format"
          → creates Sales_Report_Q2_2026.pptx (artifact art-1)
  Turn 2: "can you make better ppt design"
          → should detect follow-up, refine art-1, NOT re-query data,
            NOT re-ask clarifying questions
  Turn 3: "i need dark theme"
          → should inherit everything, just re-render the deck dark

Before the fix, turns 2 and 3 were context-blind: the planner never saw
the conversation history, and the response generator asked 5+4 clarifying
questions instead of acting.

This test drives the real parse_task_spec + generate_plan with mocked LLM
calls and a fake conversation context, asserting the follow-up wiring
end-to-end.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)


class _FakeQuery:
    def __init__(self, first_result=None, all_result=None):
        self._first = first_result
        self._all = all_result

    def filter(self, *a, **kw): return self
    def order_by(self, *a, **kw): return self
    def limit(self, *a, **kw): return self
    def first(self): return self._first
    def all(self): return self._all or []


class _FakeConv:
    def __init__(self, messages):
        self.messages = messages


class _FakeArtifact:
    def __init__(self, id, title, artifact_type):
        self.id = id
        self.title = title
        self.artifact_type = artifact_type
        from datetime import datetime
        self.created_date = datetime.utcnow()


class _FakeExecution:
    def __init__(self, task_spec, state="done"):
        self.task_spec = task_spec
        self.current_state = state


class _FakeDB:
    def __init__(self, conv, artifacts, execution=None):
        self._conv = conv
        self._artifacts = artifacts
        self._execution = execution

    def query(self, model):
        name = getattr(model, "__name__", "")
        if name == "AgentConversation":
            return _FakeQuery(first_result=self._conv)
        if name == "Artifact":
            return _FakeQuery(all_result=self._artifacts)
        if name == "Execution":
            return _FakeQuery(first_result=self._execution)
        return _FakeQuery()


class TestFollowUpE2E(unittest.TestCase):
    """Three-turn conversation regression test."""

    def test_three_turn_followup_scenario(self):
        from app.services.synexia.context_assembler import build_conversation_context
        from app.services.synexia.task_spec_parser import parse_task_spec
        from app.services.synexia.plan_dag import generate_plan

        # ── Turn 1: create sales report pptx ──────────────────────────
        # After turn 1, the conversation has user + assistant messages,
        # an artifact exists, and the prior execution has entities.
        conv_msgs_t2 = [
            {"role": "user", "content": "make sales report fro me and give me in ppt format"},
            {"role": "assistant", "content": "I've created Sales_Report_Q2_2026.pptx.",
             "tool_calls": [{"name": "sandbox"}], "artifact_ids": ["art-1"]},
            {"role": "user", "content": "can you make better ppt design"},
        ]
        artifacts = [_FakeArtifact("art-1", "Sales_Report_Q2_2026.pptx", "pptx")]
        prior_exec = _FakeExecution(task_spec={
            "entities": {"date_range": "Q2 2026", "metric": "sales"},
            "task_kind": "create_artifact",
            "artifact_intents": ["pptx"],
        })
        db_t2 = _FakeDB(
            conv=_FakeConv(conv_msgs_t2),
            artifacts=artifacts,
            execution=prior_exec,
        )

        # Build context for turn 2.
        ctx_t2 = build_conversation_context(db_t2, "conv-1", "data_analyst")
        self.assertIn("Sales_Report_Q2_2026.pptx", ctx_t2["transcript"])
        self.assertEqual(ctx_t2["recent_artifacts"][0]["id"], "art-1")
        self.assertEqual(ctx_t2["prior_entities"]["metric"], "sales")

        # Turn 2: parse_task_spec should detect follow-up.
        with patch("app.services.llm_service.call_llm", new_callable=MagicMock) as mock_goal:
            mock_goal.return_value = {"response": json.dumps({
                "task_kind": "create_artifact",
                "artifact_intents": ["pptx"],
                "entities": {"design": "better"},
                "kpis": [],
                "complexity": "simple",
                "requires_data": False,
                "is_followup": True,
                "refines_artifact_id": None,
            })}
            spec_t2 = parse_task_spec(
                "can you make better ppt design",
                agent_name="data_analyst",
                conversation_context=ctx_t2,
            )

        self.assertTrue(spec_t2["is_followup"])
        self.assertEqual(spec_t2["refines_artifact_id"], "art-1")
        # Prior entities inherited.
        self.assertEqual(spec_t2["entities"]["metric"], "sales")
        self.assertEqual(spec_t2["entities"]["design"], "better")

        # Turn 2: generate_plan should include follow-up rules (no data re-query).
        with patch("app.services.llm_service.call_llm", new_callable=MagicMock) as mock_plan:
            mock_plan.return_value = {"response": json.dumps([
                {"node_type": "sandbox", "name": "Redesign pptx",
                 "description": "Rebuild with improved design",
                 "dependencies": [], "expected_output": "pptx",
                 "output_artifact_type": "pptx"},
            ])}
            generate_plan(
                db=MagicMock(), execution_id="exec-2",
                task_spec=spec_t2,
                context_manifest={"conversation_context": ctx_t2},
                agent_name="data_analyst",
            )
            plan_prompt = mock_plan.call_args.kwargs.get("prompt", "")

        self.assertIn("Refine artifact id=art-1", plan_prompt)
        self.assertIn("do NOT re-query data", plan_prompt)

        # ── Turn 3: "i need dark theme" ───────────────────────────────
        conv_msgs_t3 = conv_msgs_t2 + [
            {"role": "assistant", "content": "Redesigned the presentation.",
             "tool_calls": [{"name": "sandbox"}], "artifact_ids": ["art-2"]},
            {"role": "user", "content": "i need dark theme"},
        ]
        artifacts_t3 = [
            _FakeArtifact("art-2", "Sales_Report_Q2_2026_v2.pptx", "pptx"),
            _FakeArtifact("art-1", "Sales_Report_Q2_2026.pptx", "pptx"),
        ]
        prior_exec_t3 = _FakeExecution(task_spec=spec_t2)
        db_t3 = _FakeDB(
            conv=_FakeConv(conv_msgs_t3),
            artifacts=artifacts_t3,
            execution=prior_exec_t3,
        )

        ctx_t3 = build_conversation_context(db_t3, "conv-1", "data_analyst")
        # Transcript now shows both prior turns.
        self.assertIn("dark theme", ctx_t3["transcript"])

        with patch("app.services.llm_service.call_llm", new_callable=MagicMock) as mock_goal3:
            mock_goal3.return_value = {"response": json.dumps({
                "task_kind": "create_artifact",
                "artifact_intents": ["pptx"],
                "entities": {"theme": "dark"},
                "kpis": [],
                "complexity": "simple",
                "requires_data": False,
                "is_followup": True,
                "refines_artifact_id": None,
            })}
            spec_t3 = parse_task_spec(
                "i need dark theme",
                agent_name="data_analyst",
                conversation_context=ctx_t3,
            )

        self.assertTrue(spec_t3["is_followup"])
        # Entities from turn 2 carried forward + new theme.
        self.assertEqual(spec_t3["entities"]["metric"], "sales")
        self.assertEqual(spec_t3["entities"]["theme"], "dark")
        # Refines the most recent artifact.
        self.assertEqual(spec_t3["refines_artifact_id"], "art-2")


if __name__ == "__main__":
    unittest.main()
