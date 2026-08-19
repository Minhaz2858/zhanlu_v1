"""Fix 2a/2b: superseded ask_data_agent results never shape artifact payloads.

A query that returned empty rows (or errored) and was later replaced by a
re-query on the same bound KB is marked ``__superseded`` by the v3 loop.
The mining functions must drop those results (default) so deck/card
generation and the methodology citation never reference a dead query.
"""

from __future__ import annotations

from app.services import generation_orchestrator as orch


def _tc(rows=None, *, error=None, source_name="catalog_sales", kb_id="kb-1",
        superseded=False):
    """Build a tool_calls_for_frontend entry shaped like the v3 loop's record."""
    res = {"source_name": source_name, "kb_id": kb_id}
    if rows is not None:
        res["rows"] = rows
    if error:
        res["error"] = error
        res["success"] = False
    tc = {"name": "ask_data_agent", "results": res}
    if superseded:
        tc["__superseded"] = True
    return tc


def test_mine_rows_skips_superseded_empty_result():
    calls = [
        _tc(rows=[], superseded=True),   # empty query replaced by a re-query
        _tc(rows=[{"a": 1}]),            # the live result
    ]
    rows = orch._mine_ask_data_rows(calls)
    assert rows == [{"a": 1}]


def test_mine_rows_skips_superseded_error_result():
    calls = [
        _tc(rows=None, error="boom", superseded=True),
        _tc(rows=[{"a": 1}]),
    ]
    assert orch._mine_ask_data_rows(calls) == [{"a": 1}]


def test_mine_rows_skip_superseded_false_returns_all():
    calls = [
        _tc(rows=[{"a": 1}], superseded=True),
        _tc(rows=[{"b": 2}]),
    ]
    assert orch._mine_ask_data_rows(calls, skip_superseded=False) == [{"a": 1}, {"b": 2}]


def test_mine_result_skips_superseded():
    calls = [
        _tc(rows=[{"a": 1}], superseded=True),
        _tc(rows=[{"a": 1}, {"a": 2}]),
    ]
    best = orch._mine_ask_data_result(calls)
    assert best is not None
    assert best["rows"] == [{"a": 1}, {"a": 2}]


def test_mine_result_superseded_jackpot_payload_does_not_win():
    """A superseded result carrying a stale report_card_payload must not win."""
    calls = [
        _tc(rows=[], superseded=True),
        _tc(rows=[{"x": 1}, {"x": 2}]),
    ]
    calls[0]["results"]["report_card_payload"] = {"summary": "stale"}
    best = orch._mine_ask_data_result(calls)
    assert best is not None
    assert "report_card_payload" not in best
    assert best["rows"] == [{"x": 1}, {"x": 2}]


def test_mine_result_non_superseded_jackpot_still_wins():
    calls = [
        _tc(rows=[{"x": 1}, {"x": 2}]),
        _tc(rows=[{"y": 1}]),
    ]
    calls[0]["results"]["report_card_payload"] = {"summary": "verified"}
    best = orch._mine_ask_data_result(calls)
    assert best["report_card_payload"] == {"summary": "verified"}


def test_mine_all_superseded_returns_none():
    calls = [_tc(rows=[], superseded=True), _tc(rows=None, error="x", superseded=True)]
    assert orch._mine_ask_data_result(calls) is None
    assert orch._mine_ask_data_rows(calls) == []
