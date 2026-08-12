"""Tests for Phase 2: rich report_synthesis wired into the FSM synthesize path.

Covers (1) the pure FinalizeResult→result_data converter, (2) the synthesize
node wiring (rich path + legacy fallback), (3) FINALIZE surfacing the
ReportCardPayload for non-file data tasks.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.synexia.contracts import (
    FinalizeResult, ReportCardPayload, KPISpec, ChartSpec, InsightSpec,
)
from app.services.synexia import capability_router as cr


def _finalize_result(*, with_payload=True, prose="Q2 revenue was 189M CNY."):
    rcp = None
    if with_payload:
        rcp = ReportCardPayload(
            title="Q2 Sales Report",
            source="sales_db",
            summary="Q2 revenue 189M CNY, Steel leads at 55%.",
            kpis=[KPISpec(label="Revenue", value="189M CNY")],
            chart=ChartSpec(type="bar", title="Revenue by material",
                            x_key="material", y_keys=["revenue"],
                            data=[{"material": "Steel", "revenue": 120}],
                            unit="CNY"),
            insights=[InsightSpec(icon="trending-up", text="Steel leads at 55%")],
        )
    return FinalizeResult(
        task_kind="report",
        assistant_content=prose,
        report_card_payload=rcp,
        user_signal="default",
    )


class TestFinalizeResultToResultData:
    def test_converts_payload_to_result_data_shape(self):
        fr = _finalize_result()
        rd = cr._finalize_result_to_result_data(fr)
        # backward-compat keys the sandbox node reads
        assert "summary" in rd and "instructions" in rd and "synth_data" in rd
        assert rd["report_card_payload"] is not None
        # sandbox reads synth_data["chart"]["data"]
        assert rd["synth_data"]["chart"]["data"] == [{"material": "Steel", "revenue": 120}]
        assert rd["synth_data"]["kpis"][0]["label"] == "Revenue"
        assert rd["synth_data"]["title"] == "Q2 Sales Report"
        # instructions fall back to the prose summary
        assert "189M CNY" in rd["instructions"]
        # report_card_payload is the serialized payload dict
        assert rd["report_card_payload"]["title"] == "Q2 Sales Report"

    def test_no_payload_produces_empty_synth_and_none_rcp(self):
        fr = _finalize_result(with_payload=False, prose="Just prose summary.")
        rd = cr._finalize_result_to_result_data(fr)
        assert rd["report_card_payload"] is None
        assert rd["synth_data"]["kpis"] == []
        assert rd["synth_data"]["chart"] is None
        assert rd["summary"] == "Just prose summary."
        assert rd["instructions"] == "Just prose summary."


def _nl2sql_prev_obs():
    return SimpleNamespace(
        success=True, observation_type="nl2sql",
        result_data={
            "sql": "SELECT material, revenue FROM sales WHERE q='Q2'",
            "data": [{"material": "Steel", "revenue": 120}],
        },
    )


def _node():
    return SimpleNamespace(name="synthesize", node_type="synthesize", inputs={})


def _execution():
    return SimpleNamespace(
        id="exec-1", user_message="summarize Q2 sales",
        task_spec={"entities": {"source_name": "sales_db", "source_id": "src-1"}},
    )


class TestSynthesizeNodeRichPath:
    def test_calls_synthesize_report_and_records_converted_result(self, monkeypatch):
        captured = {}

        async def fake_synthesize(*, user_message, rows, sql, source_name,
                                  source_id, call_llm_fn, user_signal=None,
                                  skill_name=None, skill_methodology=None):
            captured.update(rows=rows, sql=sql, source_name=source_name,
                            source_id=source_id, user_message=user_message,
                            skill_name=skill_name, skill_methodology=skill_methodology)
            return _finalize_result()

        monkeypatch.setattr(cr, "_get_all_data_observations", lambda db, ex: [_nl2sql_prev_obs()])
        monkeypatch.setattr(cr, "_get_latest_skill_observation", lambda db, ex: None)
        monkeypatch.setattr("app.services.synexia.report_synthesis.synthesize_report", fake_synthesize)
        recorded = {}
        def fake_record(db, ex, node, **kw):
            recorded.update(kw)
            return SimpleNamespace(**kw)
        monkeypatch.setattr(cr, "_record_observation", fake_record)

        out = cr._execute_synthesize_node(MagicMock(), _execution(), _node())

        # synthesize_report received rows/sql sourced from the prior nl2sql obs
        assert captured["rows"] == [{"material": "Steel", "revenue": 120}]
        assert captured["sql"].startswith("SELECT material")
        assert captured["source_name"] == "sales_db"
        assert captured["skill_name"] is None
        # recorded result_data is the converted shape (rich, with report_card_payload)
        assert recorded["result_data"]["report_card_payload"] is not None
        assert recorded["result_data"]["synth_data"]["chart"]["data"] == [{"material": "Steel", "revenue": 120}]
        assert recorded["observation_type"] == "synthesize"
        assert out.success is True

    def test_no_previous_observation_passes_empty_rows(self, monkeypatch):
        captured = {}
        async def fake_synthesize(*, user_message, rows, sql, source_name,
                                  source_id, call_llm_fn, user_signal=None,
                                  skill_name=None, skill_methodology=None):
            captured.update(rows=rows, sql=sql)
            return _finalize_result()
        monkeypatch.setattr(cr, "_get_all_data_observations", lambda db, ex: [])
        monkeypatch.setattr(cr, "_get_latest_skill_observation", lambda db, ex: None)
        monkeypatch.setattr("app.services.synexia.report_synthesis.synthesize_report", fake_synthesize)
        monkeypatch.setattr(cr, "_record_observation", lambda db, ex, node, **kw: SimpleNamespace(**kw))
        cr._execute_synthesize_node(MagicMock(), _execution(), _node())
        assert captured["rows"] == []
        assert captured["sql"] is None


class TestSynthesizeNodeFallback:
    def test_rich_path_exception_falls_back_to_legacy(self, monkeypatch):
        async def boom(**kw):
            raise RuntimeError("synthesis exploded")
        monkeypatch.setattr(cr, "_get_all_data_observations", lambda db, ex: [_nl2sql_prev_obs()])
        monkeypatch.setattr("app.services.synexia.report_synthesis.synthesize_report", boom)
        # legacy fallback calls call_llm (async) — stub it.
        async def fake_call_llm(**kw):
            return {"response": '{"title":"FB","summary":"fb summary","instructions":"do x","kpis":[],"chart":null,"insights":[]}'}
        monkeypatch.setattr("app.services.llm_service.call_llm", fake_call_llm)
        recorded = {}
        def fake_record(db, ex, node, **kw):
            recorded.update(kw)
            return SimpleNamespace(**kw)
        monkeypatch.setattr(cr, "_record_observation", fake_record)

        cr._execute_synthesize_node(MagicMock(), _execution(), _node())

        # legacy fallback produced a result (no report_card_payload)
        assert recorded["result_data"]["report_card_payload"] is None
        assert recorded["result_data"]["synth_data"]["title"] == "FB"
        assert recorded["result_data"]["summary"] == "fb summary"


def _synthesize_obs(rcp=None):
    return SimpleNamespace(
        observation_type="synthesize", success=True, tool_name="synthesizer",
        result_data={"summary": "s", "instructions": "i", "synth_data": {},
                     "report_card_payload": rcp},
    )


class TestSelectFinalizeReportCardPayload:
    """FINALIZE surfacing logic (spec §4.2): file-export artifact payload wins;
    otherwise surface the synthesize node's structured payload for non-file
    data tasks."""

    def test_non_file_task_surfaces_synthesize_rcp(self):
        rcp = {"title": "Q2 Sales", "kpis": [{"label": "Revenue", "value": "189M"}]}
        out = cr._select_finalize_report_card_payload([_synthesize_obs(rcp)], None)
        assert out == rcp

    def test_artifact_payload_wins_over_synthesize(self):
        artifact_rcp = {"title": "Exported DOCX"}
        synth_rcp = {"title": "Synth (must NOT win)"}
        out = cr._select_finalize_report_card_payload(
            [_synthesize_obs(synth_rcp)], artifact_rcp,
        )
        assert out == artifact_rcp

    def test_no_synthesize_obs_returns_none(self):
        tool_obs = SimpleNamespace(observation_type="tool_call", success=True,
                                   result_data={})
        out = cr._select_finalize_report_card_payload([tool_obs], None)
        assert out is None

    def test_synthesize_obs_without_rcp_returns_none(self):
        out = cr._select_finalize_report_card_payload([_synthesize_obs(None)], None)
        assert out is None

    def test_skips_failed_synthesize_obs(self):
        failed = SimpleNamespace(observation_type="synthesize", success=False,
                                 result_data={"report_card_payload": {"title": "x"}})
        good = _synthesize_obs({"title": "good"})
        out = cr._select_finalize_report_card_payload([failed, good], None)
        assert out == {"title": "good"}
