"""Tests for Phase 3b: synthesize aggregates ALL data observations (G5).

_get_all_data_observations returns every nl2sql observation chronologically;
synthesize combines their rows so multi-step retrieval isn't lost. Single-obs
plans behave identically to the prior last-only behavior.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.synexia import capability_router as cr


def _nl2sql_obs(seq, rows, sql="SELECT 1"):
    return SimpleNamespace(
        seq=seq, success=True, observation_type="nl2sql",
        result_data={"sql": sql, "data": rows},
    )


class _QueryStub:
    """Mimic db.query(...).filter(...).order_by(...).all() chains."""
    def __init__(self, items):
        self._items = items

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return list(self._items)


class TestGetAllDataObservations:
    def test_returns_query_result(self):
        o1 = _nl2sql_obs(1, [{"a": 1}])
        o2 = _nl2sql_obs(3, [{"a": 2}])
        db = MagicMock()
        db.query.return_value = _QueryStub([o1, o2])
        out = cr._get_all_data_observations(db, SimpleNamespace(id="e1"))
        assert out == [o1, o2]

    def test_empty_when_none(self):
        db = MagicMock()
        db.query.return_value = _QueryStub([])
        out = cr._get_all_data_observations(db, SimpleNamespace(id="e1"))
        assert out == []


class TestSynthesizeAggregatesMultipleObs:
    def test_combines_rows_from_all_nl2sql_obs(self, monkeypatch):
        captured = {}

        async def fake_synthesize(*, user_message, rows, sql, source_name,
                      source_id, call_llm_fn, user_signal=None,
                      skill_name=None, skill_methodology=None):
            captured.update(rows=rows, sql=sql)
            from app.services.synexia.contracts import FinalizeResult
            return FinalizeResult(task_kind="report", assistant_content="prose",
                                  report_card_payload=None, user_signal="default")

        monkeypatch.setattr("app.services.synexia.report_synthesis.synthesize_report", fake_synthesize)
        monkeypatch.setattr(cr, "_record_observation",
                            lambda db, ex, node, **kw: SimpleNamespace(**kw))
        monkeypatch.setattr(cr, "_get_all_data_observations", lambda db, ex: [
            _nl2sql_obs(1, [{"material": "Steel", "revenue": 120}], "SELECT a"),
            _nl2sql_obs(3, [{"material": "Copper", "revenue": 30}], "SELECT b"),
        ])
        cr._execute_synthesize_node(MagicMock(), SimpleNamespace(
            id="e1", user_message="summarize",
            task_spec={"entities": {}}), SimpleNamespace(name="synth", inputs={}))
        # rows from BOTH observations are combined
        assert {"material": "Steel", "revenue": 120} in captured["rows"]
        assert {"material": "Copper", "revenue": 30} in captured["rows"]
        assert len(captured["rows"]) == 2
        # sql joined across observations
        assert "SELECT a" in captured["sql"] and "SELECT b" in captured["sql"]

    def test_single_obs_behaves_identically(self, monkeypatch):
        captured = {}

        async def fake_synthesize(*, user_message, rows, sql, source_name,
                      source_id, call_llm_fn, user_signal=None,
                      skill_name=None, skill_methodology=None):
            captured.update(rows=rows, sql=sql)
            from app.services.synexia.contracts import FinalizeResult
            return FinalizeResult(task_kind="report", assistant_content="prose",
                                  report_card_payload=None, user_signal="default")

        monkeypatch.setattr("app.services.synexia.report_synthesis.synthesize_report", fake_synthesize)
        monkeypatch.setattr(cr, "_record_observation",
                            lambda db, ex, node, **kw: SimpleNamespace(**kw))
        monkeypatch.setattr(cr, "_get_all_data_observations",
                            lambda db, ex: [_nl2sql_obs(1, [{"x": 1}], "SELECT s")])
        cr._execute_synthesize_node(MagicMock(), SimpleNamespace(
            id="e1", user_message="m", task_spec={"entities": {}}),
            SimpleNamespace(name="synth", inputs={}))
        assert captured["rows"] == [{"x": 1}]
        assert captured["sql"] == "SELECT s"
