"""Phase 9 reliability (audit D3): automation runs must not ship a report
that was synthesized without any successful datasource call.

Historical incident: an automation made ZERO datasource queries, fabricated
a business report from the injected memory ledger (stale figures), and the
run ended "completed". The gate contract (mirrors the proven 2026-09-11
implementation):

* ``_summarize_tool_outcomes`` tallies DATA calls separately — a payload
  carrying executed-query evidence (``rows`` key or ``executed: true``);
  schema/metadata tools (``describe_schema``, ``list_data_sources``) are
  excluded because they describe capability, they do not ground figures.
* ``_should_fail_for_ungrounded_report`` fires (retryable failure) when a
  data-expected run (task pins a datasource OR the run attempted data
  tools) produced a substantive report with ZERO successful data calls.
  Honest failure explanations ("could not reach the warehouse") are NOT
  fabricated reports and pass the gate.
"""
import pytest

from app.services.automation_executor import (
    _should_fail_for_ungrounded_report,
    _summarize_tool_outcomes,
)


# ── data-call tally in _summarize_tool_outcomes ──────────────────────────

def _tool_msg(call_id, payload):
    import json
    return {"role": "tool", "tool_call_id": call_id,
            "content": json.dumps(payload)}


def test_successful_query_counts_as_data_call():
    msgs = [_tool_msg("c1", {"success": True, "rows": [["a"]], "sql": "SELECT 1"})]
    out = _summarize_tool_outcomes(msgs)
    assert out["data_calls"] == 1
    assert out["data_failures"] == 0


def test_failed_query_counts_as_data_failure():
    msgs = [_tool_msg("c1", {"success": False, "rows": [], "sql": "SELECT 1",
                             "error": "connection timed out"})]
    out = _summarize_tool_outcomes(msgs)
    assert out["data_calls"] == 1
    assert out["data_failures"] == 1


def test_schema_tools_are_not_data_calls():
    msgs = [
        _tool_msg("c1", {"success": True, "schema": {"t": ["a", "b"]}}),
        _tool_msg("c2", {"success": True, "sources": ["kb-1"]}),
    ]
    out = _summarize_tool_outcomes(msgs)
    assert out["calls"] == 2
    assert out["data_calls"] == 0


def test_executed_flag_counts_even_without_rows_key():
    msgs = [_tool_msg("c1", {"success": True, "executed": True, "row_count": 0})]
    out = _summarize_tool_outcomes(msgs)
    assert out["data_calls"] == 1


def test_persisted_v3_shape_counts_data_calls():
    msgs = [{"role": "assistant", "tool_calls": [
        {"id": "t1", "name": "query_database",
         "results": {"success": True, "rows": [[1, 2]]}},
        {"id": "t2", "name": "describe_schema",
         "results": {"success": True, "schema": {}}},
    ]}]
    out = _summarize_tool_outcomes(msgs)
    assert out["calls"] == 2
    assert out["data_calls"] == 1


def test_mixed_run_tallies_both():
    msgs = [
        _tool_msg("c1", {"success": True, "rows": [[1]]}),
        _tool_msg("c2", {"success": False, "rows": [], "sql": "SELECT 2",
                         "error": "syntax error"}),
        _tool_msg("c3", {"success": True, "note": "wrote a file"}),
    ]
    out = _summarize_tool_outcomes(msgs)
    assert out["calls"] == 3
    assert out["data_calls"] == 2
    assert out["data_failures"] == 1


# ── the gate ─────────────────────────────────────────────────────────────

REPORT = ("# Monthly Sales Report\n\nOctober revenue was ¥4.2M across "
          "the East region, up 12% month over month.")


def test_gate_fires_when_data_pinned_and_no_data_calls():
    outcome = {"calls": 3, "failures": 0, "data_calls": 0, "data_failures": 0,
               "errors": []}
    assert _should_fail_for_ungrounded_report(REPORT, outcome, data_expected=True)


def test_gate_fires_when_all_data_calls_failed():
    outcome = {"calls": 2, "failures": 2, "data_calls": 2, "data_failures": 2,
               "errors": ["connection timed out"]}
    assert _should_fail_for_ungrounded_report(REPORT, outcome, data_expected=True)


def test_gate_fires_when_data_attempted_but_none_succeeded():
    outcome = {"calls": 1, "failures": 0, "data_calls": 1, "data_failures": 1,
               "errors": []}
    assert _should_fail_for_ungrounded_report(REPORT, outcome, data_expected=False)


def test_gate_passes_honest_failure_explanation():
    outcome = {"calls": 2, "failures": 2, "data_calls": 2, "data_failures": 2,
               "errors": ["connection timed out"]}
    honest = ("I could not complete the report — the warehouse was "
              "unreachable (connection timed out). No figures are available.")
    assert not _should_fail_for_ungrounded_report(honest, outcome, data_expected=True)


def test_gate_passes_when_data_call_succeeded():
    outcome = {"calls": 3, "failures": 1, "data_calls": 2, "data_failures": 1,
               "errors": []}
    assert not _should_fail_for_ungrounded_report(REPORT, outcome, data_expected=True)


def test_gate_passes_for_non_data_tasks():
    outcome = {"calls": 2, "failures": 0, "data_calls": 0, "data_failures": 0,
               "errors": []}
    assert not _should_fail_for_ungrounded_report(REPORT, outcome, data_expected=False)


def test_gate_passes_on_empty_output():
    outcome = {"calls": 1, "failures": 1, "data_calls": 1, "data_failures": 1,
               "errors": ["x"]}
    assert not _should_fail_for_ungrounded_report("", outcome, data_expected=True)
