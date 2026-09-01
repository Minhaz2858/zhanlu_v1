"""Truthfulness gate for automation runs (item 5).

Bug: a scheduled/manual run whose EVERY tool call failed (e.g. the
MySQL data source was unreachable, SQL syntax errors) was still marked
``completed`` — because the LLM produced no content and the v3 stream
emitted the canned fallback "I've completed the requested changes.
Please review the agent configuration above." (an Agent-Builder message)
as the run output. The chat then confirmed a "successful delivery" that
never happened.

Fix (executor-side, per the run pipeline):
- ``agents._EMPTY_CONTENT_FALLBACK`` names the canned text (single
  source of truth).
- ``_summarize_tool_outcomes`` counts tool calls/failures in the run
  conversation (a fresh conversation per run → all tool messages belong
  to this run).
- ``_should_fail_for_total_tool_failure`` decides: all tools failed AND
  output is empty/canned → the run FAILED; the executor marks it failed
  with the real tool errors (retryable), instead of shipping boilerplate
  as success.
"""
import importlib
import json
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


def _executor():
    from app.services import automation_executor
    return importlib.reload(automation_executor)


def _tool_msg(payload: dict, call_id: str = "c1") -> dict:
    return {"role": "tool", "content": json.dumps(payload), "tool_call_id": call_id}


# -- _summarize_tool_outcomes -------------------------------------------------

def test_summarize_counts_calls_and_failures():
    ax = _executor()
    msgs = [
        {"role": "user", "content": "sync the data"},
        {"role": "assistant", "content": "", "tool_calls": []},
        _tool_msg({"success": False, "error": "connection to 10.10.10.49 refused"}, "c1"),
        _tool_msg({"success": False, "error": "SQL syntax error near 'rows'"}, "c2"),
        _tool_msg({"success": True, "rows": []}, "c3"),
        {"role": "assistant", "content": "final"},
    ]
    out = ax._summarize_tool_outcomes(msgs)
    assert out["calls"] == 3
    assert out["failures"] == 2
    assert any("refused" in e for e in out["errors"])
    assert any("syntax" in e for e in out["errors"])


def test_summarize_no_tool_calls():
    ax = _executor()
    out = ax._summarize_tool_outcomes([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "done"},
    ])
    assert out == {"calls": 0, "failures": 0, "errors": []}


def test_summarize_tolerates_non_json_tool_content():
    ax = _executor()
    out = ax._summarize_tool_outcomes([
        _tool_msg({"success": False}, "c1"),
        {"role": "tool", "content": "not json at all", "tool_call_id": "c2"},
    ])
    assert out["calls"] == 2
    assert out["failures"] == 1  # non-JSON content is not a failure by itself


def _assistant_tc_msg(tool_calls: list, content: str = "") -> dict:
    return {"role": "assistant", "content": content, "tool_calls": tool_calls}


def test_summarize_embedded_results_shape():
    """The v3 stream persists tool outcomes EMBEDDED in the assistant
    message's tool_calls (``{"id", "name", "arguments_string", "results"}``)
    — no role="tool" messages. The summarizer must read those too, else it
    sees calls=0 and the gate is blind (observed on a live run)."""
    ax = _executor()
    msgs = [
        {"role": "user", "content": "sync"},
        _assistant_tc_msg([
            {"id": "call_1", "name": "describe_schema",
             "arguments_string": "{}", "results": {"success": False, "error": "2003: Can't connect to MySQL"}},
            {"id": "call_2", "name": "execute_query",
             "arguments_string": "{}", "results": {"success": False, "error": "SQL syntax error near 'rows'"}},
            {"id": "call_3", "name": "create_artifact",
             "arguments_string": "{}", "results": {"success": True, "artifact_id": "a1"}},
        ]),
    ]
    out = ax._summarize_tool_outcomes(msgs)
    assert out["calls"] == 3
    assert out["failures"] == 2
    assert any("MySQL" in e for e in out["errors"])
    assert any("syntax" in e for e in out["errors"])


def test_summarize_dedupes_repeated_tool_call_snapshots():
    """The stream persists the SAME tool_calls list on every assistant
    message (snapshot per loop iteration — 10 identical messages observed
    live). Dedupe by tool-call id so counts reflect reality."""
    ax = _executor()
    tcs = [
        {"id": "call_1", "name": "describe_schema",
         "arguments_string": "{}", "results": {"success": False, "error": "boom"}},
        {"id": "call_2", "name": "create_artifact",
         "arguments_string": "{}", "results": {"success": True}},
    ]
    msgs = [{"role": "user", "content": "sync"}] + [
        _assistant_tc_msg(tcs) for _ in range(10)
    ]
    out = ax._summarize_tool_outcomes(msgs)
    assert out["calls"] == 2, f"expected dedupe by id, got {out['calls']}"
    assert out["failures"] == 1


def test_summarize_mixed_wire_and_embedded_shapes():
    """role=tool messages and embedded results in the same conversation
    must not double-count a call that appears in both shapes."""
    ax = _executor()
    msgs = [
        {"role": "user", "content": "go"},
        _assistant_tc_msg([
            {"id": "call_1", "name": "t", "arguments_string": "{}",
             "results": {"success": False, "error": "x"}},
        ]),
        _tool_msg({"success": False, "error": "x"}, "call_1"),  # same call, wire shape
        _tool_msg({"success": True}, "call_2"),
    ]
    out = ax._summarize_tool_outcomes(msgs)
    assert out["calls"] == 2
    assert out["failures"] == 1


# -- _is_canned_fallback_output ----------------------------------------------

def test_canned_fallback_detected_via_shared_constant():
    ax = _executor()
    from app.routers import agents as agents_mod
    # The constant must exist and be the text the stream actually emits.
    assert agents_mod._EMPTY_CONTENT_FALLBACK.startswith("I've completed")
    assert ax._is_canned_fallback_output(agents_mod._EMPTY_CONTENT_FALLBACK)
    assert ax._is_canned_fallback_output(
        "I've completed the requested changes. Please review the agent configuration above."
    )


def test_canned_fallback_rejects_real_output():
    ax = _executor()
    assert not ax._is_canned_fallback_output("Synced 42 rows from ERP. No anomalies found.")
    assert not ax._is_canned_fallback_output("")
    assert not ax._is_canned_fallback_output(None)


# -- _should_fail_for_total_tool_failure --------------------------------------

def _outcome(calls, failures, errors=()):
    return {"calls": calls, "failures": failures, "errors": list(errors)}


def test_gate_fails_run_when_all_tools_failed_and_output_is_canned():
    ax = _executor()
    assert ax._should_fail_for_total_tool_failure(
        "I've completed the requested changes. Please review the agent configuration above.",
        _outcome(3, 3, ["connection refused"]),
    )


def test_gate_fails_run_when_all_tools_failed_and_output_empty():
    ax = _executor()
    assert ax._should_fail_for_total_tool_failure("", _outcome(2, 2, ["boom"]))
    assert ax._should_fail_for_total_tool_failure("(no response)", _outcome(2, 2, ["boom"]))


def test_gate_passes_when_some_tool_succeeded():
    ax = _executor()
    assert not ax._should_fail_for_total_tool_failure(
        "I've completed the requested changes. Please review the agent configuration above.",
        _outcome(3, 2, ["boom"]),
    )


def test_gate_passes_when_all_failed_but_agent_wrote_substantive_output():
    """Conservative: if the LLM wrote a REAL answer (e.g. honestly
    explaining the outage), don't override its completion — the output
    already reflects what happened."""
    ax = _executor()
    assert not ax._should_fail_for_total_tool_failure(
        "I could not reach the ERP database (connection refused), so no "
        "rows were synced this run. Please check the data source.",
        _outcome(2, 2, ["connection refused"]),
    )


def test_gate_passes_without_tool_calls():
    ax = _executor()
    assert not ax._should_fail_for_total_tool_failure(
        "I've completed the requested changes. Please review the agent configuration above.",
        _outcome(0, 0),
    )


# -- _tool_warnings_line: partial failures stay visible ------------------------

def test_warnings_line_empty_when_no_failures():
    ax = _executor()
    assert ax._tool_warnings_line(None) == ""
    assert ax._tool_warnings_line(_outcome(5, 0)) == ""
    assert ax._tool_warnings_line({"calls": 0, "failures": 0, "errors": []}) == ""


def test_warnings_line_surfaces_count_and_errors():
    ax = _executor()
    line = ax._tool_warnings_line(
        _outcome(17, 1, ["describe_schema failed: Can't connect to MySQL server on '10.10.10.49'"])
    )
    assert "1 of 17" in line
    assert "MySQL" in line


def test_warnings_line_caps_error_count():
    ax = _executor()
    line = ax._tool_warnings_line(_outcome(4, 3, ["e1", "e2", "e3"]))
    assert "e1" in line and "e2" in line
    assert "e3" not in line  # capped at 2 errors inline
