"""Tests for the multi-agent FSM file-export pipeline.

Covers:
1. task_spec_parser — deterministic file-intent injection
2. plan_dag._build_default_plan — multi-agent 3-node DAG
3. capability_router._execute_synthesize_node — LLM summary
4. finalize.fsm_finalize_into_artifact — builds payload from observations
"""

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
# Layer 1 — Goal Engine: task_spec_parser
# ---------------------------------------------------------------------------


class TestTaskSpecParserFileIntent(unittest.TestCase):
    """parse_task_spec must inject detect_file_intent result deterministically."""

    def _make_mock_llm(self, return_payload: dict):
        """Create a mock call_llm that returns a fixed payload."""
        return MagicMock(return_value={"response": json.dumps(return_payload)})

    @patch("app.services.llm_service.call_llm", new_callable=MagicMock)
    def test_docx_file_intent_injected(self, mock_call_llm):
        """When user asks for docx, artifact_intents=['docx'] and user_signal='export_docx'."""
        mock_call_llm.return_value = {
            "response": json.dumps({
                "task_kind": "create_artifact",
                "artifact_intents": [],
                "entities": {"report_title": "Q2 Report"},
                "kpis": ["accuracy"],
                "complexity": "moderate",
                "requires_data": True,
            })
        }
        from app.services.synexia.task_spec_parser import parse_task_spec

        result = parse_task_spec("Create a quarterly business report in docx", agent_name="test_agent")

        self.assertEqual(result["task_kind"], "create_artifact")
        self.assertIn("docx", result["artifact_intents"])
        self.assertEqual(result["user_signal"], "export_docx")
        self.assertTrue(result["requires_data"])

    @patch("app.services.llm_service.call_llm", new_callable=MagicMock)
    def test_no_file_intent_no_change(self, mock_call_llm):
        """When no format is mentioned, artifact_intents and user_signal keep LLM values."""
        mock_call_llm.return_value = {
            "response": json.dumps({
                "task_kind": "analyze_data",
                "artifact_intents": ["chart"],
                "entities": {"date_range": "Q1 2026"},
                "kpis": ["completeness"],
                "complexity": "moderate",
                "requires_data": True,
                "user_signal": "default",
            })
        }
        from app.services.synexia.task_spec_parser import parse_task_spec

        result = parse_task_spec("Show me Q1 2026 sales trends", agent_name="test_agent")

        self.assertEqual(result["task_kind"], "analyze_data")
        self.assertNotIn("export_", result.get("user_signal", ""))
        self.assertEqual(result["user_signal"], "default")

    @patch("app.services.llm_service.call_llm", new_callable=MagicMock)
    def test_llm_fails_default_has_file_intent(self, mock_call_llm):
        """When LLM call fails, the fallback dict still has the detected file intent."""
        mock_call_llm.side_effect = Exception("LLM timeout")
        from app.services.synexia.task_spec_parser import parse_task_spec

        result = parse_task_spec("give me a pptx deck", agent_name="test_agent")

        self.assertIn("pptx", result.get("artifact_intents", []))
        self.assertEqual(result["user_signal"], "export_pptx")
        self.assertTrue(result["requires_data"])

    @patch("app.services.llm_service.call_llm", new_callable=MagicMock)
    def test_llm_returns_different_intent_overridden(self, mock_call_llm):
        """When LLM returns wrong artifact_intents, the deterministic override wins."""
        mock_call_llm.return_value = {
            "response": json.dumps({
                "task_kind": "general",
                "artifact_intents": ["chart"],
                "entities": {},
                "kpis": [],
                "complexity": "simple",
                "requires_data": False,
                "user_signal": "default",
            })
        }
        from app.services.synexia.task_spec_parser import parse_task_spec

        result = parse_task_spec("make me a xlsx spreadsheet with the numbers", agent_name="test_agent")

        self.assertIn("xlsx", result["artifact_intents"])
        # Should include both the LLM's 'chart' and the deterministic 'xlsx'
        self.assertIn("chart", result["artifact_intents"])
        self.assertEqual(result["user_signal"], "export_xlsx")
        self.assertTrue(result["requires_data"])
        self.assertEqual(result["task_kind"], "create_artifact")

    @patch("app.services.llm_service.call_llm", new_callable=MagicMock)
    def test_selected_skill_sets_override_metadata(self, mock_call_llm):
        """A runtime-selected custom skill must be preserved on TaskSpec
        and must suppress default-skill auto-pick metadata."""
        mock_call_llm.return_value = {
            "response": json.dumps({
                "task_kind": "create_artifact",
                "artifact_intents": [],
                "entities": {},
                "kpis": [],
                "complexity": "moderate",
                "requires_data": True,
                "user_signal": "default",
            })
        }
        from app.services.synexia.task_spec_parser import parse_task_spec

        selected_skill = {
            "id": "tool-weekly-sales",
            "name": "weekly-sales-report",
            "trigger": "/weekly-sales-report",
        }
        result = parse_task_spec(
            "make a pptx weekly sales report",
            agent_name="test_agent",
            active_skill=selected_skill,
        )

        self.assertTrue(result["skill_override"])
        self.assertIsNone(result["auto_picked_default"])
        self.assertEqual(result["selected_skill_id"], "tool-weekly-sales")
        self.assertEqual(result["selected_skill_name"], "weekly-sales-report")
        self.assertEqual(result["selected_skill"], selected_skill)


# ---------------------------------------------------------------------------
# Layer 2 — Planning Engine: plan_dag._build_default_plan
# ---------------------------------------------------------------------------


class TestBuildDefaultPlanMultiAgent(unittest.TestCase):
    """_build_default_plan must emit a 3-node DAG for file-format requests."""

    def _spec(self, **overrides) -> dict:
        base = {
            "task_kind": "create_artifact",
            "artifact_intents": ["docx"],
            "entities": {"date_range": "Q2 2026", "metric": "revenue"},
            "kpis": [],
            "complexity": "moderate",
            "requires_data": True,
            "user_signal": "export_docx",
        }
        base.update(overrides)
        return base

    def test_file_format_emits_3_node_dag(self):
        from app.services.synexia.plan_dag import _build_default_plan
        steps = _build_default_plan(self._spec(), agent_name="test_agent")

        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[0]["node_type"], "nl2sql")
        self.assertEqual(steps[0]["agent_role"], "data_analyst")
        self.assertEqual(steps[1]["node_type"], "synthesize")
        self.assertEqual(steps[1]["agent_role"], "synthesizer")
        self.assertEqual(steps[2]["node_type"], "sandbox")
        self.assertEqual(steps[2]["agent_role"], "presenter")

    def test_dependencies_chain(self):
        from app.services.synexia.plan_dag import _build_default_plan
        steps = _build_default_plan(self._spec(), agent_name="test_agent")

        # nl2sql has no deps, synthesize depends on [0], sandbox depends on [1]
        self.assertEqual(steps[0]["dependencies"], [])
        self.assertEqual(steps[1]["dependencies"], [0])
        self.assertEqual(steps[2]["dependencies"], [1])

    def test_file_format_no_data_emits_2_node_dag(self):
        """When requires_data=False, skip nl2sql — emit only synthesize + sandbox."""
        spec = self._spec(requires_data=False)
        from app.services.synexia.plan_dag import _build_default_plan
        steps = _build_default_plan(spec, agent_name="test_agent")

        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["node_type"], "synthesize")
        self.assertEqual(steps[1]["node_type"], "sandbox")
        self.assertEqual(steps[1]["dependencies"], [0])

    def test_non_file_plan_unchanged(self):
        """When user_signal is 'default', emit the existing analysis-only plan."""
        from app.services.synexia.plan_dag import _build_default_plan
        steps = _build_default_plan({
            "task_kind": "analyze_data",
            "artifact_intents": [],
            "requires_data": True,
            "user_signal": "default",
        }, agent_name="test_agent")

        # Should be the old 1-node nl2sql plan
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["node_type"], "nl2sql")

    def test_non_file_plan_no_data(self):
        """When no data needed and no file format, emit fallback tool plan."""
        from app.services.synexia.plan_dag import _build_default_plan
        steps = _build_default_plan({
            "task_kind": "answer_question",
            "artifact_intents": [],
            "requires_data": False,
            "user_signal": "default",
        }, agent_name="test_agent")

        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["node_type"], "tool")


# ---------------------------------------------------------------------------
# Layer 2 — contracts: PlanNodeSpec.agent_role
# ---------------------------------------------------------------------------


class TestPlanNodeSpecAgentRole(unittest.TestCase):
    """PlanNodeSpec must accept the new agent_role field."""

    def test_agent_role_default_none(self):
        from app.services.synexia.contracts import PlanNodeSpec

        spec = PlanNodeSpec(node_id="n1", node_type="nl2sql", name="Query")
        self.assertIsNone(spec.agent_role)

    def test_agent_role_set(self):
        from app.services.synexia.contracts import PlanNodeSpec

        spec = PlanNodeSpec(
            node_id="n1", node_type="nl2sql", name="Query",
            agent_role="data_analyst",
        )
        self.assertEqual(spec.agent_role, "data_analyst")

    def test_agent_role_serializes(self):
        from app.services.synexia.contracts import PlanNodeSpec

        spec = PlanNodeSpec(
            node_id="n1", node_type="synthesize", name="Summarize",
            agent_role="synthesizer",
        )
        d = spec.model_dump()
        self.assertEqual(d["agent_role"], "synthesizer")


# ---------------------------------------------------------------------------
# Layer 3 — Capability Router: _execute_synthesize_node
# ---------------------------------------------------------------------------


class TestExecuteSynthesizeNode(unittest.TestCase):
    """_execute_synthesize_node must produce a summary observation."""

    def _make_mock_node(self, seq=0):
        node = MagicMock()
        node.id = f"node-{seq}"
        node.seq = seq
        node.name = "Write report summary"
        node.node_type = "synthesize"
        node.inputs = {}
        return node

    def _make_mock_execution(self, seq=0, user_message="Q2 report in docx"):
        execution = MagicMock()
        execution.id = f"exec-{seq}"
        execution.user_message = user_message
        execution.task_spec = {
            "task_kind": "create_artifact",
            "artifact_intents": ["docx"],
            "entities": {"date_range": "Q2 2026", "metric": "revenue"},
            "user_signal": "export_docx",
        }
        return execution

    def test_synthesize_node_returns_observation(self):
        from app.services.synexia.capability_router import _execute_synthesize_node
        from app.services.synexia.contracts import FinalizeResult, ReportCardPayload

        db = MagicMock()
        execution = self._make_mock_execution()
        node = self._make_mock_node()

        async def fake_synthesize(**kwargs):
            return FinalizeResult(
                task_kind="report",
                assistant_content="Revenue grew 15% YoY driven by top-3 materials.",
                report_card_payload=ReportCardPayload(
                    title="Q2 2026 Revenue Report",
                    summary="Revenue grew 15% YoY driven by top-3 materials.",
                ),
                user_signal="export_docx",
            )

        with patch("app.services.synexia.report_synthesis.synthesize_report", fake_synthesize):
            obs = _execute_synthesize_node(db, execution, node)

        self.assertTrue(obs.success)
        self.assertEqual(obs.observation_type, "synthesize")
        self.assertIn("summary", obs.result_data)
        self.assertEqual(obs.result_data["summary"], "Revenue grew 15% YoY driven by top-3 materials.")
        self.assertIn("instructions", obs.result_data)
        self.assertIn("synth_data", obs.result_data)

    @patch("app.services.llm_service.call_llm", new_callable=MagicMock)
    def test_synthesize_llm_failure_fallback(self, mock_call_llm):
        """When LLM fails, the synthesize node still returns a fallback observation."""
        mock_call_llm.side_effect = Exception("LLM unavailable")

        from app.services.synexia.capability_router import _execute_synthesize_node

        db = MagicMock()
        execution = self._make_mock_execution()
        node = self._make_mock_node()

        obs = _execute_synthesize_node(db, execution, node)

        self.assertTrue(obs.success)
        self.assertEqual(obs.observation_type, "synthesize")
        self.assertIsNotNone(obs.result_data)


# ---------------------------------------------------------------------------
# Layer 3 — finalize: fsm_finalize_into_artifact
# ---------------------------------------------------------------------------


class TestFsmFinalizeIntoArtifact(unittest.TestCase):
    """fsm_finalize_into_artifact must build a ReportCardPayload from observations."""

    def _make_obs(self, observation_type: str, result_data: dict, success=True):
        obs = MagicMock()
        obs.observation_type = observation_type
        obs.success = success
        obs.result_data = result_data
        return obs

    @patch("app.services.synexia.finalize.finalize_into_artifact")
    def test_builds_payload_from_synthesize_observation(self, mock_finalize):
        mock_finalize.return_value = (MagicMock(id="art-123"), {"docx": {"artifact_id": "art-456"}})

        from app.services.synexia.finalize import fsm_finalize_into_artifact

        db = MagicMock()
        observations = [
            self._make_obs("synthesize", {
                "summary": "Revenue grew 15% YoY.",
                "instructions": "Generate professional DOCX.",
                "synth_data": {
                    "title": "Q2 2026 Revenue Report",
                    "summary": "Revenue grew 15% YoY.",
                    "kpis": [
                        {"label": "Total Revenue", "value": "189.3M CNY", "caption": "Top 7"},
                    ],
                    "chart": {
                        "type": "bar",
                        "title": "Revenue by Material",
                        "x_key": "material_name",
                        "y_keys": ["total_revenue"],
                        "data": [{"material_name": "Resin A", "total_revenue": 1000}],
                        "unit": "CNY",
                    },
                    "insights": [
                        {"icon": "trending-up", "text": "Top 3 materials = 76%."},
                    ],
                },
            }),
            self._make_obs("sandbox", {
                "artifact_id": "art-456",
                "format": "docx",
            }),
        ]

        result = fsm_finalize_into_artifact(
            db,
            conversation_id="conv-1",
            agent_name="test_agent",
            user_message="Q2 report in docx",
            observations=observations,
            task_spec={"user_signal": "export_docx"},
        )

        self.assertIsNotNone(result)
        artifact, file_exports, payload = result
        self.assertEqual(payload.title, "Q2 2026 Revenue Report")
        self.assertEqual(payload.user_signal, "export_docx")
        self.assertEqual(len(payload.kpis), 1)
        self.assertEqual(payload.kpis[0].label, "Total Revenue")
        self.assertIsNotNone(payload.chart)
        self.assertEqual(payload.chart.type, "bar")
        self.assertEqual(len(payload.insights), 1)
        self.assertEqual(payload.insights[0].text, "Top 3 materials = 76%.")

    @patch("app.services.synexia.finalize.finalize_into_artifact")
    def test_no_synthesize_observation_returns_none(self, mock_finalize):
        from app.services.synexia.finalize import fsm_finalize_into_artifact

        db = MagicMock()
        result = fsm_finalize_into_artifact(
            db,
            conversation_id="conv-1",
            agent_name="test_agent",
            user_message="hello",
            observations=[self._make_obs("nl2sql", {"sql": "SELECT 1"})],
            task_spec={},
        )

        self.assertIsNone(result)
        mock_finalize.assert_not_called()

    @patch("app.services.synexia.finalize.finalize_into_artifact")
    def test_passes_through_to_finalize_into_artifact(self, mock_finalize):
        mock_finalize.return_value = (MagicMock(id="art-789"), {"docx": {"artifact_id": "art-789"}})

        from app.services.synexia.finalize import fsm_finalize_into_artifact

        db = MagicMock()
        observations = [
            self._make_obs("synthesize", {
                "summary": "Test summary.",
                "instructions": "Test instructions.",
                "synth_data": {"title": "Test Report", "kpis": [], "insights": []},
            }),
        ]

        fsm_finalize_into_artifact(
            db,
            conversation_id="conv-1",
            agent_name="test_agent",
            user_message="test report in docx",
            observations=observations,
        )

        mock_finalize.assert_called_once()
        call_kwargs = mock_finalize.call_args[1]
        self.assertIn("payload", call_kwargs)
        self.assertEqual(call_kwargs["payload"].title, "Test Report")
        self.assertEqual(call_kwargs["conversation_id"], "conv-1")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
