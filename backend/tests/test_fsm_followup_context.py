"""Unit tests for follow-up conversation context wiring across the FSM.

Covers:
1. build_conversation_context — compact transcript, recent artifacts, prior entities
2. parse_task_spec — follow-up detection (is_followup, refines_artifact_id) + entity inheritance
3. generate_plan — planner prompt consumes context_manifest (no longer a black hole)
4. _build_response_prompt — includes transcript + one-question clarification policy
5. is_followup_refinement — routing-layer detector for short refinement turns
6. format_followup_context_block — legacy ReAct system-prompt context block
7. FSM _run_goal — reuses router-supplied conversation_context (no duplicate DB query)
8. agents.py routing override — source-level wiring checks for v2/v3 chat routes
"""

from __future__ import annotations

import ast
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)


# ---------------------------------------------------------------------------
# Fake DB helpers — emulate the query chains used by context_assembler
# without standing up a real database.
# ---------------------------------------------------------------------------


class _FakeQuery:
    """Minimal query-chain stub: filter/order_by/limit are no-ops."""

    def __init__(self, first_result=None, all_result=None):
        self._first = first_result
        self._all = all_result

    def filter(self, *a, **kw):
        return self

    def order_by(self, *a, **kw):
        return self

    def limit(self, *a, **kw):
        return self

    def first(self):
        return self._first

    def all(self):
        return self._all or []


class _FakeConv:
    def __init__(self, messages, dashboard_id=None):
        self.messages = messages
        self.dashboard_id = dashboard_id


class _FakeArtifact:
    def __init__(self, id, title, artifact_type, created_date=None):
        self.id = id
        self.title = title
        self.artifact_type = artifact_type
        self.created_date = created_date


class _FakeExecution:
    def __init__(self, task_spec, current_state="done"):
        self.task_spec = task_spec
        self.current_state = current_state


class _FakeDB:
    """Routes .query(model) to the right fake result by model __name__."""

    def __init__(self, conv=None, artifacts=None, execution=None):
        self._conv = conv
        self._artifacts = artifacts
        self._execution = execution

    def query(self, model):
        name = getattr(model, "__name__", "")
        key = getattr(model, "key", "")
        if key == "dashboard_id":
            return _FakeQuery(first_result=(self._conv.dashboard_id,) if self._conv and self._conv.dashboard_id else None)
        if name == "AgentConversation":
            return _FakeQuery(first_result=self._conv)
        if name == "Artifact":
            return _FakeQuery(all_result=self._artifacts)
        if name == "Execution":
            return _FakeQuery(first_result=self._execution)
        return _FakeQuery()


# ---------------------------------------------------------------------------
# 1. build_conversation_context
# ---------------------------------------------------------------------------


class TestBuildConversationContext(unittest.TestCase):
    def test_returns_transcript_artifacts_and_entities(self):
        from app.services.synexia.context_assembler import build_conversation_context

        conv = _FakeConv(messages=[
            {"role": "user", "content": "make a sales report in ppt"},
            {"role": "assistant", "content": "Created Sales_Report.pptx",
             "tool_calls": [{"name": "sandbox"}], "artifact_ids": ["art-1"]},
        ])
        artifacts = [_FakeArtifact("art-1", "Sales_Report.pptx", "pptx")]
        prev_exec = _FakeExecution(task_spec={
            "entities": {"date_range": "Q2 2026", "metric": "revenue"},
        })
        db = _FakeDB(conv=conv, artifacts=artifacts, execution=prev_exec)

        ctx = build_conversation_context(db, "conv-1", "data_analyst")

        self.assertIn("User: make a sales report", ctx["transcript"])
        self.assertIn("Assistant: Created Sales_Report.pptx", ctx["transcript"])
        self.assertEqual(len(ctx["recent_artifacts"]), 1)
        self.assertEqual(ctx["recent_artifacts"][0]["id"], "art-1")
        self.assertEqual(ctx["prior_entities"]["metric"], "revenue")

    def test_returns_empty_when_no_conversation_id(self):
        from app.services.synexia.context_assembler import build_conversation_context

        self.assertEqual(build_conversation_context(MagicMock(), None, "x"), {})

    def test_includes_bound_dashboard_id(self):
        from app.services.synexia.context_assembler import build_conversation_context

        conv = _FakeConv(messages=[], dashboard_id="dash-1")
        db = _FakeDB(conv=conv)

        ctx = build_conversation_context(db, "conv-1", "data_analyst")

        self.assertEqual(ctx["dashboard_id"], "dash-1")

    def test_non_fatal_on_db_error(self):
        from app.services.synexia.context_assembler import build_conversation_context

        broken_db = MagicMock()
        broken_db.query.side_effect = RuntimeError("db down")
        # Must not raise — degrades to empty context.
        self.assertEqual(build_conversation_context(broken_db, "conv-1", "x"), {})

    def test_transcript_caps_long_messages(self):
        from app.services.synexia.context_assembler import build_conversation_context

        long_content = "x" * 5000
        conv = _FakeConv(messages=[{"role": "user", "content": long_content}])
        db = _FakeDB(conv=conv)

        ctx = build_conversation_context(db, "conv-1", "x")
        # Each message capped to ~600 chars + ellipsis.
        self.assertLess(len(ctx["transcript"]), 700)


# ---------------------------------------------------------------------------
# 2. parse_task_spec — follow-up detection + entity inheritance
# ---------------------------------------------------------------------------


class TestParseTaskSpecFollowup(unittest.TestCase):
    def _mock_llm(self, payload: dict):
        return MagicMock(return_value={"response": json.dumps(payload)})

    @patch("app.services.llm_service.call_llm", new_callable=MagicMock)
    def test_followup_fields_default_to_false_without_context(self, mock_llm):
        mock_llm.return_value = {"response": json.dumps({
            "task_kind": "create_artifact",
            "artifact_intents": [],
            "entities": {},
            "kpis": [],
            "complexity": "simple",
            "requires_data": False,
        })}
        from app.services.synexia.task_spec_parser import parse_task_spec

        result = parse_task_spec("make a report", agent_name="test")
        self.assertFalse(result["is_followup"])
        self.assertIsNone(result["refines_artifact_id"])

    @patch("app.services.llm_service.call_llm", new_callable=MagicMock)
    def test_followup_detected_and_entities_inherited(self, mock_llm):
        """When the LLM says is_followup=true and context has prior entities,
        the TaskSpec merges prior entities and pins refines_artifact_id."""
        mock_llm.return_value = {"response": json.dumps({
            "task_kind": "create_artifact",
            "artifact_intents": ["pptx"],
            "entities": {"style": "dark"},
            "kpis": [],
            "complexity": "simple",
            "requires_data": False,
            "is_followup": True,
            "refines_artifact_id": None,
        })}
        from app.services.synexia.task_spec_parser import parse_task_spec

        ctx = {
            "transcript": "User: make sales report ppt\nAssistant: done",
            "recent_artifacts": [{"id": "art-1", "title": "Sales.pptx", "artifact_type": "pptx"}],
            "prior_entities": {"date_range": "Q2 2026", "metric": "revenue"},
        }
        result = parse_task_spec(
            "i need dark theme", agent_name="test", conversation_context=ctx,
        )

        self.assertTrue(result["is_followup"])
        # refines_artifact_id defaulted to most-recent artifact when LLM left it null.
        self.assertEqual(result["refines_artifact_id"], "art-1")
        # Entities merged: prior + current (current wins on conflict).
        self.assertEqual(result["entities"]["metric"], "revenue")
        self.assertEqual(result["entities"]["style"], "dark")

    @patch("app.services.llm_service.call_llm", new_callable=MagicMock)
    def test_followup_hint_injected_for_refinement_verbs(self, mock_llm):
        """A short refinement message + prior artifact → prompt contains the hint."""
        mock_llm.return_value = {"response": json.dumps({
            "task_kind": "general", "artifact_intents": [], "entities": {},
            "kpis": [], "complexity": "simple", "requires_data": False,
            "is_followup": True,
        })}
        from app.services.synexia.task_spec_parser import parse_task_spec

        ctx = {
            "transcript": "User: make ppt\nAssistant: done",
            "recent_artifacts": [{"id": "art-1", "title": "X", "artifact_type": "pptx"}],
            "prior_entities": {},
        }
        parse_task_spec("make it better", agent_name="test", conversation_context=ctx)

        prompt = mock_llm.call_args.kwargs.get("prompt", "")
        self.assertIn("follow-up", prompt.lower())
        self.assertIn("Conversation so far", prompt)


# ---------------------------------------------------------------------------
# 3. generate_plan — planner prompt consumes context_manifest
# ---------------------------------------------------------------------------


class TestGeneratePlanConsumesContext(unittest.TestCase):
    @patch("app.services.llm_service.call_llm", new_callable=MagicMock)
    def test_plan_prompt_contains_transcript_and_artifacts(self, mock_llm):
        """generate_plan must inject the conversation context block into the
        LLM prompt — previously context_manifest was accepted but never read."""
        mock_llm.return_value = {"response": json.dumps([
            {"node_type": "sandbox", "name": "Rebuild", "description": "d",
             "dependencies": [], "expected_output": "pptx"},
        ])}
        from app.services.synexia.plan_dag import generate_plan

        context_manifest = {
            "conversation_context": {
                "transcript": "User: make sales ppt\nAssistant: done",
                "recent_artifacts": [
                    {"id": "art-1", "title": "Sales.pptx", "artifact_type": "pptx"},
                ],
                "prior_entities": {"metric": "revenue"},
            }
        }
        fake_db = MagicMock()
        generate_plan(
            db=fake_db, execution_id="exec-1",
            task_spec={"task_kind": "create_artifact", "artifact_intents": ["pptx"],
                        "entities": {}, "user_signal": "export_pptx",
                        "is_followup": True, "refines_artifact_id": "art-1"},
            context_manifest=context_manifest,
            agent_name="test",
        )

        prompt = mock_llm.call_args.kwargs.get("prompt", "")
        self.assertIn("Conversation context", prompt)
        self.assertIn("Sales.pptx", prompt)
        # Follow-up rules present when is_followup=True.
        self.assertIn("Refine artifact id=art-1", prompt)

    @patch("app.services.llm_service.call_llm", new_callable=MagicMock)
    def test_plan_prompt_no_context_block_when_manifest_empty(self, mock_llm):
        mock_llm.return_value = {"response": json.dumps([
            {"node_type": "tool", "name": "x", "description": "d",
             "dependencies": [], "expected_output": "text"},
        ])}
        from app.services.synexia.plan_dag import generate_plan

        generate_plan(
            db=MagicMock(), execution_id="exec-1",
            task_spec={"task_kind": "general", "artifact_intents": [],
                        "entities": {}, "user_signal": "default"},
            context_manifest={},
            agent_name="test",
        )
        prompt = mock_llm.call_args.kwargs.get("prompt", "")
        self.assertNotIn("Conversation context", prompt)


# ---------------------------------------------------------------------------
# 4. _build_response_prompt — transcript + clarification policy
# ---------------------------------------------------------------------------


class TestBuildResponsePrompt(unittest.TestCase):
    def _make_fsm(self, context_manifest=None, task_spec=None):
        from app.services.synexia.fsm import SynexiaFSM

        fsm = SynexiaFSM.__new__(SynexiaFSM)
        fsm.db = MagicMock()

        class _Obs:
            def __init__(self, success, tool_name, result_text, error_message=""):
                self.success = success
                self.tool_name = tool_name
                self.observation_type = "tool_call"
                self.result_text = result_text
                self.error_message = error_message

        class _Exec:
            pass

        exec_inst = _Exec()
        exec_inst.observations = [_Obs(True, "sandbox", "generated pptx")]
        exec_inst.context_manifest = context_manifest or {}
        exec_inst.task_spec = task_spec or {}

        fsm.execution = exec_inst
        return fsm

    def test_prompt_includes_transcript_when_context_present(self):
        from app.services.synexia.fsm import ExecutionRequest

        cm = {"conversation_context": {
            "transcript": "User: make sales ppt\nAssistant: done",
            "recent_artifacts": [], "prior_entities": {},
        }}
        fsm = self._make_fsm(context_manifest=cm)
        req = ExecutionRequest(
            conversation_id="c1", agent_name="test", user_message="dark theme",
        )
        prompt = fsm._build_response_prompt(req)

        self.assertIn("Conversation so far", prompt)
        self.assertIn("make sales ppt", prompt)

    def test_prompt_includes_one_question_clarification_policy(self):
        from app.services.synexia.fsm import ExecutionRequest

        fsm = self._make_fsm()
        req = ExecutionRequest(
            conversation_id="c1", agent_name="test", user_message="x",
        )
        prompt = fsm._build_response_prompt(req)

        self.assertIn("at most ONE clarifying question", prompt)
        self.assertIn("Never re-ask", prompt)

    def test_prompt_includes_followup_note_when_is_followup(self):
        from app.services.synexia.fsm import ExecutionRequest

        fsm = self._make_fsm(
            task_spec={"is_followup": True, "refines_artifact_id": "art-1"},
        )
        req = ExecutionRequest(
            conversation_id="c1", agent_name="test", user_message="dark theme",
        )
        prompt = fsm._build_response_prompt(req)
        self.assertIn("follow-up turn", prompt)


# ---------------------------------------------------------------------------
# 5. is_followup_refinement — routing-layer detector
# ---------------------------------------------------------------------------


def _refinement_ctx():
    return {
        "transcript": "User: make a sales report in ppt\nAssistant: Created Sales_Report.pptx",
        "recent_artifacts": [{"id": "art-1", "title": "Sales_Report.pptx", "artifact_type": "pptx"}],
        "prior_entities": {"metric": "revenue"},
    }


class TestIsFollowupRefinement(unittest.TestCase):
    def test_true_for_refinement_language_with_artifact(self):
        from app.services.planning_trigger import is_followup_refinement

        self.assertTrue(is_followup_refinement("make it dark theme", _refinement_ctx()))

    def test_true_for_pronoun_with_transcript_only(self):
        """No artifact needed — a non-empty transcript is refinable too."""
        from app.services.planning_trigger import is_followup_refinement

        ctx = {"transcript": "User: make sales ppt\nAssistant: done", "recent_artifacts": []}
        self.assertTrue(is_followup_refinement("change that please", ctx))

    def test_false_without_context(self):
        from app.services.planning_trigger import is_followup_refinement

        self.assertFalse(is_followup_refinement("make it dark theme", None))
        self.assertFalse(is_followup_refinement("make it dark theme", {}))

    def test_false_when_context_has_nothing_refinable(self):
        from app.services.planning_trigger import is_followup_refinement

        ctx = {"transcript": "   ", "recent_artifacts": []}
        self.assertFalse(is_followup_refinement("make it dark theme", ctx))

    def test_false_for_long_message(self):
        from app.services.planning_trigger import is_followup_refinement

        long_msg = "dark theme " * 40  # > 300 chars — likely a new request
        self.assertFalse(is_followup_refinement(long_msg, _refinement_ctx()))

    def test_false_for_empty_message(self):
        from app.services.planning_trigger import is_followup_refinement

        self.assertFalse(is_followup_refinement("", _refinement_ctx()))
        self.assertFalse(is_followup_refinement("   ", _refinement_ctx()))

    def test_false_without_cue_words(self):
        from app.services.planning_trigger import is_followup_refinement

        self.assertFalse(is_followup_refinement("hello how are you today", _refinement_ctx()))

    def test_true_for_dashboard_breakdown_when_dashboard_is_bound(self):
        from app.services.planning_trigger import is_followup_refinement

        ctx = {
            "transcript": "User: make a weekly sales dashboard\nAssistant: Created Weekly Sales Dashboard",
            "recent_artifacts": [],
            "dashboard_id": "dash-1",
        }
        self.assertTrue(is_followup_refinement("Customer breakdown", ctx))

    def test_false_for_dashboard_breakdown_without_bound_dashboard(self):
        from app.services.planning_trigger import is_followup_refinement

        ctx = {
            "transcript": "User: what are top customers?\nAssistant: Here is a table",
            "recent_artifacts": [],
        }
        self.assertFalse(is_followup_refinement("Customer breakdown", ctx))

    def test_word_boundary_precision(self):
        """'edit' contains 'it' but must NOT match — cues are word-boundary matched."""
        from app.services.planning_trigger import is_followup_refinement

        self.assertFalse(is_followup_refinement("please edit the file", _refinement_ctx()))

    def test_non_fatal_on_malformed_context(self):
        from app.services.planning_trigger import is_followup_refinement

        # object() has no .get — must degrade to False, never raise.
        self.assertFalse(is_followup_refinement("make it dark theme", object()))

    def test_regression_original_bug_message(self):
        """The exact failing scenario: 'make it dark theme' bypasses the
        heuristic trigger (simple-conversation bypass), so the follow-up
        override must be the thing that routes it to the FSM."""
        from app.services.planning_trigger import should_trigger_planning, is_followup_refinement

        msg = "make it dark theme"
        trigger = should_trigger_planning(msg)
        # Bypass still fires (short, no connectives/plan keywords, ≤1 action verb)…
        self.assertFalse(trigger.should_plan)
        # …and the detector is what rescues the turn.
        self.assertTrue(is_followup_refinement(msg, _refinement_ctx()))


# ---------------------------------------------------------------------------
# 6. format_followup_context_block — legacy ReAct system-prompt block
# ---------------------------------------------------------------------------


class TestFormatFollowupContextBlock(unittest.TestCase):
    def test_empty_without_context(self):
        from app.services.synexia.context_assembler import format_followup_context_block

        self.assertEqual(format_followup_context_block(None), "")
        self.assertEqual(format_followup_context_block({}), "")
        self.assertEqual(
            format_followup_context_block({"transcript": "", "recent_artifacts": []}), ""
        )

    def test_renders_transcript_and_directive(self):
        from app.services.synexia.context_assembler import format_followup_context_block

        block = format_followup_context_block({
            "transcript": "User: make sales ppt\nAssistant: done",
            "recent_artifacts": [],
        })
        self.assertIn("=== Conversation so far ===", block)
        self.assertIn("make sales ppt", block)
        self.assertIn("follow-up", block)
        self.assertIn("brand-new topic", block)

    def test_renders_artifacts(self):
        from app.services.synexia.context_assembler import format_followup_context_block

        block = format_followup_context_block({
            "transcript": "",
            "recent_artifacts": [
                {"id": "art-1", "title": "Sales.pptx", "artifact_type": "pptx"},
                {"id": "art-2", "title": "NoType"},  # missing artifact_type
            ],
        })
        self.assertIn("(no prior transcript available)", block)
        self.assertIn("=== Recent artifacts (refinable) ===", block)
        self.assertIn("- art-1: Sales.pptx (pptx)", block)
        self.assertIn("- art-2: NoType", block)
        self.assertNotIn("- art-2: NoType (", block)

    def test_artifacts_capped_at_five(self):
        from app.services.synexia.context_assembler import format_followup_context_block

        ctx = {"recent_artifacts": [
            {"id": f"art-{i}", "title": f"T{i}", "artifact_type": "pptx"} for i in range(6)
        ]}
        block = format_followup_context_block(ctx)
        self.assertIn("art-4", block)
        self.assertNotIn("art-5", block)

    def test_non_fatal_on_malformed_context(self):
        from app.services.synexia.context_assembler import format_followup_context_block

        self.assertEqual(format_followup_context_block(object()), "")


# ---------------------------------------------------------------------------
# 7. FSM _run_goal — reuses router-supplied conversation_context
# ---------------------------------------------------------------------------


class TestRunGoalReusesConversationContext(unittest.TestCase):
    def _make_fsm(self):
        from app.services.synexia.fsm import SynexiaFSM

        fsm = SynexiaFSM.__new__(SynexiaFSM)
        fsm.db = MagicMock()
        fsm.execution = MagicMock()
        return fsm

    @patch("app.services.synexia.context_assembler.build_conversation_context")
    @patch("app.services.synexia.task_spec_parser.parse_task_spec")
    def test_reuses_router_supplied_context(self, mock_parse, mock_build):
        """Follow-up override path: the router already built the context, so
        GOAL must NOT query the DB again — and parse_task_spec must receive
        the supplied context."""
        from app.services.synexia.fsm import ExecutionRequest

        mock_parse.return_value = {"task_kind": "general"}
        supplied = {
            "transcript": "User: make sales ppt",
            "recent_artifacts": [{"id": "art-1"}],
            "prior_entities": {},
        }
        req = ExecutionRequest(
            conversation_id="c1", agent_name="test", user_message="dark theme",
            conversation_context=supplied,
        )
        self._make_fsm()._run_goal(req)

        mock_build.assert_not_called()
        self.assertEqual(
            mock_parse.call_args.kwargs.get("conversation_context"), supplied,
        )

    @patch("app.services.synexia.context_assembler.build_conversation_context")
    @patch("app.services.synexia.task_spec_parser.parse_task_spec")
    def test_builds_context_when_not_supplied(self, mock_parse, mock_build):
        """Direct FSM entry (no router): GOAL builds the context itself."""
        from app.services.synexia.fsm import ExecutionRequest

        mock_parse.return_value = {"task_kind": "general"}
        built = {"transcript": "User: hi", "recent_artifacts": [], "prior_entities": {}}
        mock_build.return_value = built
        req = ExecutionRequest(
            conversation_id="c1", agent_name="test", user_message="hello there friend",
        )
        self._make_fsm()._run_goal(req)

        mock_build.assert_called_once()
        self.assertEqual(
            mock_parse.call_args.kwargs.get("conversation_context"), built,
        )


# ---------------------------------------------------------------------------
# 8. agents.py routing override — source-level wiring checks (v2 + v3)
# ---------------------------------------------------------------------------


_AGENTS_PATH = os.path.join(_BACKEND_ROOT, "app", "routers", "agents.py")


def _load_agents_source():
    with open(_AGENTS_PATH) as f:
        return f.read()


def _load_route_functions():
    """Return {func_name: unparsed_source} for the two chat route handlers."""
    source = _load_agents_source()
    tree = ast.parse(source)
    funcs = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in (
            "add_message", "add_message_stream",
        ):
            funcs[node.name] = ast.unparse(node)
    return funcs


class TestRoutingOverrideWiring(unittest.TestCase):
    """Both chat routes must (a) build conversation context, (b) force
    refinement turns into the FSM via the follow-up override, (c) pass the
    context through to ExecutionRequest, and (d) inject the follow-up block
    into the legacy-loop system prompt as defense-in-depth."""

    def test_module_imports(self):
        source = _load_agents_source()
        self.assertIn("is_followup_refinement", source)
        self.assertIn("PlanTrigger", source)
        self.assertIn("format_followup_context_block", source)
        self.assertIn("build_conversation_context", source)

    def test_v2_add_message_wiring(self):
        funcs = _load_route_functions()
        self.assertIn("add_message", funcs)
        src = funcs["add_message"]
        self.assertIn("is_followup_refinement", src)
        self.assertIn("followup-override", src)
        self.assertIn("conversation_context=_conv_ctx", src)
        self.assertIn("format_followup_context_block(_conv_ctx)", src)

    def test_v3_add_message_stream_wiring(self):
        funcs = _load_route_functions()
        self.assertIn("add_message_stream", funcs)
        src = funcs["add_message_stream"]
        self.assertIn("is_followup_refinement", src)
        self.assertIn("followup-override", src)
        self.assertIn("conversation_context=_v3_conv_ctx", src)
        self.assertIn("format_followup_context_block(_v3_conv_ctx)", src)

    def _real_func_lines(self, func_def_marker):
        """Real file lines for one route function (unparse reorders — don't
        use it for ordering assertions)."""
        lines = _load_agents_source().splitlines(keepends=True)
        start = next(i for i, l in enumerate(lines) if func_def_marker in l)
        end = next(
            (i for i in range(start + 1, len(lines))
             if lines[i].startswith(("async def ", "def "))),
            len(lines),
        )
        return lines[start:end]

    def test_v3_context_built_before_trigger_and_override_before_fsm(self):
        body = self._real_func_lines("async def add_message_stream")

        def _idx(needle):
            return next(i for i, l in enumerate(body) if needle in l)

        # Context is built first so both the override check and the FSM
        # request can reuse it (single DB context query per turn).
        self.assertLess(
            _idx("_v3_conv_ctx = build_conversation_context"),
            _idx("_v3_plan_trigger = should_trigger_planning"),
        )
        self.assertLess(
            _idx("_v3_plan_trigger = should_trigger_planning"),
            _idx('source="followup-override"'),
        )
        self.assertLess(
            _idx('source="followup-override"'),
            _idx("SynexiaFSM(db)"),
        )

    def test_v2_context_built_before_trigger_and_override_before_fsm(self):
        body = self._real_func_lines("async def add_message(")

        def _idx(needle):
            return next(i for i, l in enumerate(body) if needle in l)

        self.assertLess(
            _idx("_conv_ctx = build_conversation_context"),
            _idx("_plan_trigger = should_trigger_planning"),
        )
        self.assertLess(
            _idx("_plan_trigger = should_trigger_planning"),
            _idx('source="followup-override"'),
        )
        self.assertLess(
            _idx('source="followup-override"'),
            _idx("SynexiaFSM(db)"),
        )


if __name__ == "__main__":
    unittest.main()
