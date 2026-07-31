"""Tests for the Synexia report-pipeline modules.

Covers:
  - contracts.py: Pydantic v2 typed contracts (TaskSpec, PlanDAG,
    ObservationRecord, ReportCardPayload, FinalizeResult)
  - user_signal.py: detection of "export/download" intent
  - report_synthesis.py: the forced synthesis LLM turn
  - finalize.py: artifact write + tool-call payload shape

These tests are pure-Python and don't require a live LLM.
"""

import asyncio
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


# ---------------------------------------------------------------------------
# contracts.py
# ---------------------------------------------------------------------------


class TestPlanDAGTopoSort(unittest.TestCase):
    """PlanDAG.topo_sort must return nodes in dependency order."""

    def test_linear_chain(self):
        from app.services.synexia.contracts import PlanDAG, PlanNodeSpec

        dag = PlanDAG(nodes=[
            PlanNodeSpec(node_id="a", node_type="tool", name="A"),
            PlanNodeSpec(node_id="b", node_type="tool", name="B", dependencies=["a"]),
            PlanNodeSpec(node_id="c", node_type="tool", name="C", dependencies=["b"]),
        ])
        order = [n.node_id for n in dag.topo_sort()]
        self.assertEqual(order, ["a", "b", "c"])

    def test_diamond(self):
        from app.services.synexia.contracts import PlanDAG, PlanNodeSpec

        dag = PlanDAG(nodes=[
            PlanNodeSpec(node_id="a", node_type="tool", name="A"),
            PlanNodeSpec(node_id="b", node_type="tool", name="B", dependencies=["a"]),
            PlanNodeSpec(node_id="c", node_type="tool", name="C", dependencies=["a"]),
            PlanNodeSpec(node_id="d", node_type="tool", name="D", dependencies=["b", "c"]),
        ])
        order = [n.node_id for n in dag.topo_sort()]
        self.assertEqual(order[0], "a")
        self.assertEqual(order[-1], "d")
        self.assertIn("b", order[1:3])
        self.assertIn("c", order[1:3])

    def test_independent_nodes(self):
        from app.services.synexia.contracts import PlanDAG, PlanNodeSpec

        dag = PlanDAG(nodes=[
            PlanNodeSpec(node_id="x", node_type="tool", name="X"),
            PlanNodeSpec(node_id="y", node_type="tool", name="Y"),
        ])
        order = [n.node_id for n in dag.topo_sort()]
        self.assertEqual(set(order), {"x", "y"})


class TestReportCardPayload(unittest.TestCase):
    """ReportCardPayload must round-trip through Pydantic and the model_dump."""

    def test_minimal_payload(self):
        from app.services.synexia.contracts import ReportCardPayload

        p = ReportCardPayload(title="Hello", summary="World")
        d = p.model_dump()
        self.assertEqual(d["title"], "Hello")
        self.assertEqual(d["summary"], "World")
        self.assertEqual(d["kpis"], [])
        self.assertIsNone(d["chart"])
        self.assertEqual(d["user_signal"], "default")

    def test_full_payload_roundtrip(self):
        from app.services.synexia.contracts import (
            ActionSpec,
            ChartSpec,
            InsightSpec,
            KPISpec,
            ReportCardPayload,
        )

        p = ReportCardPayload(
            title="Sales",
            source="erp_v_sale_orderentry",
            summary="Top 3 = 76%",
            kpis=[
                KPISpec(label="Total", value="189M", caption="Top 7"),
            ],
            chart=ChartSpec(
                type="bar",
                title="Top materials",
                x_key="material_name",
                y_keys=["total_revenue"],
                data=[{"material_name": "X", "total_revenue": 1.0}],
                unit="CNY",
            ),
            insights=[InsightSpec(icon="trending-up", text="Concentration risk.")],
            next_step="Break it down by region.",
            actions=[ActionSpec(label="Save", prompt="Save as weekly.")],
            user_signal="export",
        )
        d = p.model_dump()
        # Round-trip back into the model
        p2 = ReportCardPayload.model_validate(d)
        self.assertEqual(p2.title, p.title)
        self.assertEqual(p2.user_signal, "export")
        self.assertEqual(len(p2.insights), 1)
        self.assertEqual(p2.chart.data[0]["material_name"], "X")


# ---------------------------------------------------------------------------
# user_signal.py
# ---------------------------------------------------------------------------


class TestDetectUserSignal(unittest.TestCase):
    """detect_user_signal is a hot-path check — it must be fast and right."""

    def test_default_for_normal_questions(self):
        from app.services.synexia.user_signal import detect_user_signal

        for msg in [
            "can you make a sales report for me?",
            "show me the top customers",
            "what did we sell last quarter?",
            "hi",
            "",
        ]:
            self.assertEqual(detect_user_signal(msg), "default", msg=f"failed for: {msg!r}")

    def test_export_keyword(self):
        from app.services.synexia.user_signal import detect_user_signal

        for msg in [
            "export this as PDF",
            "download the report",
            "save it as XLSX",
            "send me the PPT deck",
            "give me an excel spreadsheet",
            "I need this as a file",
        ]:
            self.assertEqual(detect_user_signal(msg), "export", msg=f"failed for: {msg!r}")

    def test_case_insensitive(self):
        from app.services.synexia.user_signal import detect_user_signal

        self.assertEqual(detect_user_signal("EXPORT this"), "export")
        self.assertEqual(detect_user_signal("Download"), "export")

    def test_word_boundary_no_false_positive(self):
        """'exports' (the noun) should still trigger because the regex
        matches it; 'exported' should also trigger.  We just want to
        make sure the regex doesn't blow up on weird input."""
        from app.services.synexia.user_signal import detect_user_signal

        # These SHOULD trigger
        self.assertEqual(detect_user_signal("exported last year"), "export")
        # Empty / None safe
        self.assertEqual(detect_user_signal(None), "default")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# report_synthesis.py
# ---------------------------------------------------------------------------


class TestSynthesisFallback(unittest.TestCase):
    """If the LLM fails or returns no JSON, we must still produce a payload."""

    def test_fallback_with_rows(self):
        from app.services.synexia.contracts import ReportCardPayload
        from app.services.synexia.report_synthesis import _fallback_payload

        rows = [
            {"material_name": "碳五石油树脂", "total_revenue": 66_400_000.0},
            {"material_name": "异戊二烯",     "total_revenue": 22_100_000.0},
            {"material_name": "间戊二烯",     "total_revenue": 18_000_000.0},
        ]
        p = _fallback_payload(
            user_message="make sales report",
            rows=rows,
            sql="SELECT ...",
            source_name="db_zhanlu_no1",
            user_signal="default",
        )
        self.assertIsInstance(p, ReportCardPayload)
        self.assertEqual(len(p.kpis), 3)
        self.assertGreater(p.kpis[0].value.find("M"), -1,
                           "KPI value should be formatted in millions")
        self.assertIsNotNone(p.chart)
        self.assertEqual(p.chart.x_key, "material_name")
        self.assertEqual(p.chart.data, rows)
        # Top performer should appear in insights
        self.assertTrue(any("碳五石油树脂" in i.text for i in p.insights),
                        f"Expected top performer in insights: {p.insights}")

    def test_fallback_with_empty_rows(self):
        from app.services.synexia.contracts import ReportCardPayload
        from app.services.synexia.report_synthesis import _fallback_payload

        p = _fallback_payload(
            user_message="make report",
            rows=[],
            sql=None,
            source_name="db",
            user_signal="default",
        )
        self.assertIsInstance(p, ReportCardPayload)
        self.assertTrue(any("0" in k.value for k in p.kpis))
        self.assertTrue(any("no data" in i.text.lower() or "no actionable" in i.text.lower() for i in p.insights))


class TestExtractJsonBlock(unittest.TestCase):
    """We must pull the first JSON fence out of the LLM reply."""

    def test_fenced_json(self):
        from app.services.synexia.report_synthesis import _extract_json_block, _strip_json_block

        text = (
            "Here is the report.\n"
            "```json\n{\"title\": \"X\", \"summary\": \"y\"}\n```\n"
            "Anything after the fence."
        )
        block = _extract_json_block(text)
        self.assertIsNotNone(block)
        self.assertEqual(block["title"], "X")
        self.assertIn("Here is the report.", _strip_json_block(text))
        self.assertNotIn("```", _strip_json_block(text))

    def test_no_fence(self):
        from app.services.synexia.report_synthesis import _extract_json_block

        self.assertIsNone(_extract_json_block("Just some prose, no JSON."))

    def test_bare_json(self):
        from app.services.synexia.report_synthesis import _extract_json_block

        text = 'Prefix prose\n{"title": "Bare"}'
        block = _extract_json_block(text)
        self.assertIsNotNone(block)
        self.assertEqual(block["title"], "Bare")


class TestSynthesizeReport(unittest.TestCase):
    """synthesize_report is the synthesis LLM turn. It must:

    1. Always return a FinalizeResult.
    2. Always return a non-None report_card_payload.
    3. Use the LLM's prose as assistant_content when it returns a JSON block.
    4. Fall back to the heuristic when the LLM fails or returns no JSON.
    5. Detect user_signal from the message if not provided.
    """

    def _run(self, llm_reply, *, user_message, rows=None, sql=None, source_name=None,
             user_signal=None):
        from app.services.synexia.report_synthesis import synthesize_report
        return asyncio.run(synthesize_report(
            user_message=user_message,
            rows=rows,
            sql=sql,
            source_name=source_name,
            source_id="kb-1",
            call_llm_fn=AsyncMock(return_value={"content": llm_reply}),
            user_signal=user_signal,
        ))

    def test_llm_returns_json_and_prose(self):
        reply = (
            "Top material is 碳五石油树脂 at 66M (35% of total).\n\n"
            "```json\n"
            '{"title": "Sales report", "summary": "Top 3 = 76%", '
            '"kpis": [{"label": "Total", "value": "189M"}], '
            '"chart": {"type": "bar", "x_key": "m", "y_keys": ["r"], "data": []}, '
            '"insights": [{"icon": "trending-up", "text": "Concentration risk."}], '
            '"next_step": "Break it down by region."}\n'
            "```"
        )
        result = self._run(reply, user_message="make sales report",
                           rows=[{"m": "X", "r": 1}],
                           source_name="db_zhanlu_no1")
        self.assertEqual(result.task_kind, "report")
        self.assertIsNotNone(result.report_card_payload)
        self.assertEqual(result.report_card_payload.title, "Sales report")
        self.assertIn("碳五石油树脂", result.assistant_content)
        self.assertEqual(result.user_signal, "default")

    def test_llm_returns_no_json(self):
        result = self._run(
            "Just prose, no JSON here.",
            user_message="make report",
            rows=[{"m": "A", "r": 100}, {"m": "B", "r": 50}],
            source_name="db",
        )
        self.assertIsNotNone(result.report_card_payload)
        # The LLM's prose is preserved as assistant_content
        self.assertIn("Just prose", result.assistant_content)
        # The fallback KPIs were computed
        self.assertGreaterEqual(len(result.report_card_payload.kpis), 1)

    def test_llm_raises_exception(self):
        from app.services.synexia.report_synthesis import synthesize_report
        call = AsyncMock(side_effect=RuntimeError("LLM down"))
        result = asyncio.run(synthesize_report(
            user_message="make report",
            rows=[{"m": "A", "r": 100}],
            sql="SELECT 1",
            source_name="db",
            source_id="kb-1",
            call_llm_fn=call,
        ))
        self.assertIsNotNone(result.report_card_payload)
        self.assertTrue(any("LLM" in w or "fail" in w.lower() for w in result.warnings),
                        f"expected warning about LLM failure, got {result.warnings}")

    def test_user_signal_detected_from_message(self):
        # No user_signal kwarg — should be auto-detected from the message
        result = self._run(
            "Prose\n```json\n{\"title\": \"T\"}\n```",
            user_message="export this as PDF",
            rows=[],
        )
        self.assertEqual(result.user_signal, "export")
        self.assertEqual(result.report_card_payload.user_signal, "export")


# ---------------------------------------------------------------------------
# finalize.py
# ---------------------------------------------------------------------------


class TestFinalizePayload(unittest.TestCase):
    """build_tool_call_payload must produce a dict the frontend can render."""

    def test_payload_with_artifact(self):
        from app.services.synexia.contracts import (
            FinalizeResult,
            ReportCardPayload,
        )
        from app.services.synexia.finalize import build_tool_call_payload

        fr = FinalizeResult(
            task_kind="report",
            assistant_content="Hello",
            report_card_payload=ReportCardPayload(title="X", summary="y"),
            user_signal="default",
        )
        d = build_tool_call_payload(fr, artifact_id="art-1")
        self.assertEqual(d["type"], "report_card")
        self.assertEqual(d["task_kind"], "report")
        self.assertEqual(d["artifact_id"], "art-1")
        self.assertEqual(d["report_card_payload"]["title"], "X")

    def test_payload_without_artifact(self):
        from app.services.synexia.contracts import FinalizeResult
        from app.services.synexia.finalize import build_tool_call_payload

        fr = FinalizeResult(task_kind="report", user_signal="default")
        d = build_tool_call_payload(fr, artifact_id=None)
        self.assertNotIn("artifact_id", d)
        self.assertIsNone(d["report_card_payload"])


class TestFinalizeArtifactWrite(unittest.TestCase):
    """finalize_into_artifact must always return a FinalizeResult and try
    to write an Artifact row.  We mock the DB session so we don't need
    a real connection."""

    def test_writes_artifact_row(self):
        from app.services.synexia.contracts import ReportCardPayload
        from app.services.synexia.finalize import finalize_into_artifact

        db = MagicMock()
        db.add = MagicMock()
        db.flush = MagicMock()
        db.rollback = MagicMock()

        payload = ReportCardPayload(
            title="Sales report",
            summary="Top 3 = 76%",
            kpis=[],
            chart=None,
            insights=[],
        )
        art = finalize_into_artifact(
            db,
            conversation_id="conv-1",
            agent_name="erp_sales",
            user_message="make report",
            source="db_zhanlu_no1",
            sql="SELECT 1",
            payload=payload,
        )
        self.assertIsNotNone(art)
        self.assertEqual(art.artifact_type, "html_report")
        self.assertEqual(art.status, "preview_ready")
        self.assertIn("synexia-fsm", art.tags)
        # finalize_into_artifact now writes Artifact + ArtifactVersion + ArtifactBlob
        self.assertEqual(db.add.call_count, 3)
        self.assertGreaterEqual(db.flush.call_count, 2)

    def test_db_failure_returns_none(self):
        from app.services.synexia.contracts import ReportCardPayload
        from app.services.synexia.finalize import finalize_into_artifact

        db = MagicMock()
        db.add.side_effect = RuntimeError("db down")
        db.rollback = MagicMock()

        payload = ReportCardPayload(title="X", summary="y")
        art = finalize_into_artifact(
            db,
            conversation_id="c", agent_name="a",
            user_message="m", source="s", sql=None, payload=payload,
        )
        self.assertIsNone(art)
        db.rollback.assert_called()


if __name__ == "__main__":
    unittest.main()
