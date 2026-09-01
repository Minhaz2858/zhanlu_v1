"""Fix 3: closing-card synthesis prefers the overview, not the last drill-down.

The final card must reflect the deliverable's verified payload. When several
ask_data_agent results exist, the result with more rows/columns (the
overview) wins over a later drill-down with fewer rows, and superseded
results are excluded entirely.
"""

from __future__ import annotations

from app.services import generation_orchestrator as orch


def _tc(rows, *, source_name="catalog_sales", kb_id="kb-1", superseded=False):
    tc = {
        "name": "ask_data_agent",
        "results": {"rows": rows, "source_name": source_name, "kb_id": kb_id},
    }
    if superseded:
        tc["__superseded"] = True
    return tc


def test_mine_result_prefers_overview_even_when_drilldown_is_last():
    overview = [
        {"region": "North", "sales": 100, "target": 90},
        {"region": "South", "sales": 80, "target": 70},
    ]
    drill_down = [{"region": "North", "sales": 100}]  # fewer rows AND columns
    calls = [_tc(overview), _tc(drill_down)]
    best = orch._mine_ask_data_result(calls)
    assert best["rows"] == overview


def test_mine_result_excludes_superseded_drilldown():
    overview = [{"region": "N", "sales": 1}, {"region": "S", "sales": 2}]
    calls = [_tc(overview), _tc([{"region": "N"}], superseded=True)]
    best = orch._mine_ask_data_result(calls)
    assert best["rows"] == overview


def test_mine_result_answer_only_result_still_wins():
    """A result with an answer but zero rows is a valid fallback candidate."""
    tc = _tc([])
    tc["results"]["answer"] = "aggregated figure available"
    best = orch._mine_ask_data_result([tc])
    assert best is not None
    assert best["answer"] == "aggregated figure available"


def test_mine_result_skips_results_without_rows_or_answer():
    empty = _tc([])
    empty["results"]["answer"] = ""
    best = orch._mine_ask_data_result([empty, _tc([{"a": 1}])])
    assert best["rows"] == [{"a": 1}]
