"""Phase 9 Stage 4 — end-to-end enterprise acceptance scenarios.

Service-level workflow compositions (deterministic fixtures, mocked model
responses, zero paid LLM calls). Each scenario verifies a COMPLETE workflow
contract — not a single function:

  S1  Multi-source analysis turn: successful query evidence SURVIVES a
      later tool failure; failed tools never fabricate evidence; findings
      are provenance (compact), not raw row dumps.
  S2  Fabricated-report rejection: an automation whose datasource calls
      all failed (or that made none) cannot ship a substantive report as
      "completed" — the ungrounded gate fails it retryably, while an
      honest failure explanation ships fine.
  S3  Parallel tool batch: concurrent sync tools run on DISTINCT sessions
      (audit A1), per-call failures degrade to error dicts, and a failure
      in one call never discards a sibling's successful result.
  S4  Cross-organization isolation: a caller claim naming another org's
      agent never enters the execution context; the authenticated
      principal is the only identity source.
"""
import json

import pytest

from app.services.automation_executor import (
    _should_fail_for_ungrounded_report,
    _summarize_tool_outcomes,
)
from app.services.mcp_server.mcp_auth import (
    is_privileged_jsonrpc,
    resolve_execution_context,
)
from app.services.turn_evidence import record_turn_evidence


# ── helpers ──────────────────────────────────────────────────────────────

def _wire_tool_msg(call_id, payload):
    return {"role": "tool", "tool_call_id": call_id,
            "content": json.dumps(payload)}


def _v3_shape_call(call_id, name, status, results):
    return {"id": call_id, "name": name, "status": status, "results": results}


REPORT = ("# October Business Review\n\nRevenue reached ¥4.2M, up 12% "
          "month over month; top customer accounted for 18% of volume.")


# ── S1: evidence survival on a partially-failed multi-source turn ───────

def test_s1_evidence_survives_subsequent_tool_failure():
    tool_calls = [
        # successful multi-row query → must become evidence
        _v3_shape_call("c1", "query_database", "completed", {
            "success": True, "rows": [["acme", 100], ["glob", 80]],
            "columns": ["customer", "amount"], "row_count": 2,
            "data_source_id": "kb-74c8d56d",
        }),
        # failed delegation → never evidence
        _v3_shape_call("c2", "ask_data_agent", "failed",
                       {"success": False, "error": "connection timed out"}),
        # context tool → never evidence
        _v3_shape_call("c3", "describe_schema", "completed",
                       {"success": True, "schema": {"t": ["a"]}}),
        # a LATER failure must not invalidate the earlier evidence
        _v3_shape_call("c4", "execute_query", "failed",
                       {"success": False, "rows": [], "sql": "SELECT ...",
                        "error": "syntax error near FROM"}),
    ]
    ids = record_turn_evidence("conv-e2e-s1", tool_calls)
    assert len(ids) == 1, "exactly the successful query becomes evidence"

    from app.services.agent_collaboration.shared_evidence import get_registry
    item = get_registry().get("conv-e2e-s1", ids[0])
    assert item.findings["tool"] == "query_database"
    assert item.findings["row_count"] == 2
    assert item.findings["source_id"] == "kb-74c8d56d"
    # provenance, not a raw row dump
    assert "rows" not in item.findings
    assert "acme" not in json.dumps(item.findings)


# ── S2: fabricated-report rejection at the workflow level ────────────────

def test_s2_ungrounded_report_is_rejected_honest_failure_ships():
    # Run A: the agent tried twice to query and every data call failed,
    # then wrote a polished business report anyway → gate must reject.
    run_a_msgs = [
        _wire_tool_msg("c1", {"success": False, "rows": [], "sql": "SELECT 1",
                              "error": "Can't connect (timed out)"}),
        _wire_tool_msg("c2", {"success": False, "rows": [], "sql": "SELECT 2",
                              "error": "Can't connect (timed out)"}),
    ]
    outcome_a = _summarize_tool_outcomes(run_a_msgs)
    assert outcome_a["data_calls"] == 2 and outcome_a["data_failures"] == 2
    assert _should_fail_for_ungrounded_report(REPORT, outcome_a,
                                              data_expected=True)

    # Run B: same outage, but the agent honestly explains it → ships.
    honest = ("The report could not be completed: the warehouse was "
              "unreachable (connection timed out). No figures available "
              "for this period.")
    assert not _should_fail_for_ungrounded_report(honest, outcome_a,
                                                  data_expected=True)

    # Run C: grounded run — one data call succeeded → report ships.
    run_c_msgs = [
        _wire_tool_msg("c1", {"success": True, "rows": [["oct", 4200000]],
                              "columns": ["month", "revenue"]}),
        _wire_tool_msg("c2", {"success": False, "rows": [], "sql": "SELECT 2",
                              "error": "syntax error"}),
    ]
    outcome_c = _summarize_tool_outcomes(run_c_msgs)
    assert outcome_c["data_failures"] < outcome_c["data_calls"]
    assert not _should_fail_for_ungrounded_report(REPORT, outcome_c,
                                                  data_expected=True)


# ── S3: parallel batch — distinct sessions, honest per-call failures ────

@pytest.mark.asyncio
async def test_s3_parallel_batch_isolation_and_honest_degradation(monkeypatch):
    from app.services import agent_tools

    caller_db = object()
    session_ids = []
    counter = {"n": 0}

    def fake_sessionlocal():
        counter["n"] += 1

        class _S:
            n = counter["n"]

            def close(self):
                pass
        session_ids.append(counter["n"])
        return _S()

    monkeypatch.setattr(agent_tools, "SessionLocal", fake_sessionlocal,
                        raising=False)

    def ok_handler(args, db, user_id):
        return {"success": True, "rows": [[1]], "row_count": 1}

    def boom_handler(args, db, user_id):
        raise RuntimeError("datasource down")

    r1 = await agent_tools._execute_with_reliability(
        "query_ok", {}, ok_handler, caller_db, "u1",
        context=None, use_context=False)
    r2 = await agent_tools._execute_with_reliability(
        "query_boom", {}, boom_handler, caller_db, "u1",
        context=None, use_context=False)

    assert r1["success"] is True and r1["rows"] == [[1]]
    assert r2["success"] is False and "datasource down" in r2["error"]
    # the failing call must not have invalidated the successful one's session
    assert len(session_ids) == 2 and session_ids[0] != session_ids[1]


# ── S4: cross-org claim isolation ────────────────────────────────────────

def test_s4_cross_org_claim_never_enters_execution_context():
    principal = {"user_id": "u-ok", "org_id": "org-ok", "role": "member"}
    ctx, rest = resolve_execution_context(
        None,  # mismatch drops the context BEFORE any db access
        {"_zhanlu_context": {"org_id": "org-OTHER",
                             "agent_app_id": "agent-of-other-org"}},
        principal,
    )
    assert ctx["org_id"] == "org-ok"
    assert ctx["user_id"] == "u-ok"
    assert ctx.get("agent_app_id") is None, (
        "a cross-org agent claim entered the execution context"
    )
    assert ctx["verified"] is True
    assert rest == {}  # the reserved context arg never leaks into tool args


def test_s4_privileged_split_composite():
    assert is_privileged_jsonrpc(
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{}}') is True
    assert is_privileged_jsonrpc(
        b'[{"method":"tools/list"},{"method":"tools/call"}]') is True
    assert is_privileged_jsonrpc(
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}') is False
