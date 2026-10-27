"""P1 regression tests — orchestration + observability.

Covers tests 5, 6, 9 and 10 of
``docs/plans/2026-10-27-query-result-contract-design.md``:

  5  Repeated identical failures must trip the loop guard (RC-4: the guard
     used to count only successes, so the production 6× repeat was invisible).
  6  Three concurrent identical requests execute at most once.
  9  A UI/plan-step failure must not erase a valid query result.
  10 ``ask_data_agent`` and ``query_database`` agree on one canonical
     contract; dedup is scoped, same-turn only, and never cross-turn cached.

Deterministic doubles only — no live DB, no network.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.services.db.query_dedup import (
    InFlightDeduplicator,
    get_inflight_dedup,
    request_fingerprint,
    reset_inflight_dedup,
    set_inflight_dedup,
    sql_fingerprint,
)


# ── Test 5: loop guard counts repeated FAILURES ──────────────────────────


def test_5_repeated_identical_failures_trip_loop_guard():
    """RC-4: six identical failures must be detected.

    Before the fix the guard filtered ``if ok``, so a deterministically
    failing call repeated 6× was invisible — exactly the production shape.
    """
    from app.services.reliability import LoopState, ReliabilityConfig

    cfg = ReliabilityConfig(loop_guard_threshold=3, loop_guard_window=10)
    ls = LoopState()
    for _ in range(3):
        ls.record("query_database", {"question": "last week sales"}, success=False)

    is_loop, reason = ls.is_loop(cfg)
    assert is_loop is True
    assert "query_database" in reason
    assert "failed 3" in reason


def test_5_successes_still_trip():
    from app.services.reliability import LoopState, ReliabilityConfig

    cfg = ReliabilityConfig(loop_guard_threshold=3)
    ls = LoopState()
    for _ in range(3):
        ls.record("query_database", {"question": "q"}, success=True)
    is_loop, reason = ls.is_loop(cfg)
    assert is_loop is True
    assert "succeeded each time" in reason


def test_5_distinct_failures_do_not_trip():
    """Legitimately different failing queries are exploration, not a loop."""
    from app.services.reliability import LoopState, ReliabilityConfig

    cfg = ReliabilityConfig(loop_guard_threshold=3)
    ls = LoopState()
    for i in range(5):
        ls.record("query_database", {"question": f"q{i}"}, success=False)
    assert ls.is_loop(cfg)[0] is False


def test_5_mixed_success_and_failure_tally_separately():
    """One success + two failures of the SAME call is not yet a loop at 3,
    but three failures are — the tallies do not pollute each other."""
    from app.services.reliability import LoopState, ReliabilityConfig

    cfg = ReliabilityConfig(loop_guard_threshold=3)
    ls = LoopState()
    ls.record("t", {"a": 1}, success=True)
    ls.record("t", {"a": 1}, success=False)
    ls.record("t", {"a": 1}, success=False)
    assert ls.is_loop(cfg)[0] is False
    ls.record("t", {"a": 1}, success=False)
    assert ls.is_loop(cfg)[0] is True


# ── Test 6: concurrent identical calls execute once ──────────────────────


def test_6_three_concurrent_identical_requests_execute_once():
    async def scenario():
        dedup = InFlightDeduplicator()
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)  # simulate pipeline latency
            return {"rows": [{"a": 1}], "run": calls}

        results = await asyncio.gather(
            *[dedup.run("fp-1", factory) for _ in range(3)]
        )
        return calls, results

    calls, results = asyncio.run(scenario())

    assert calls == 1, "the pipeline must run exactly once"
    assert all(r.value == results[0].value for r in results), "identical result"
    roles = sorted(r.role for r in results)
    assert roles == ["follower", "follower", "leader"]
    assert results[0].followers == 2


def test_6_different_fingerprints_are_not_coalesced():
    async def scenario():
        dedup = InFlightDeduplicator()
        calls = 0

        async def factory(i):
            async def _f():
                nonlocal calls
                calls += 1
                await asyncio.sleep(0.01)
                return i

            return _f

        await asyncio.gather(dedup.run("a", await factory(1)), dedup.run("b", await factory(2)))
        return calls

    assert asyncio.run(scenario()) == 2


def test_6_leader_cancellation_does_not_cancel_followers_work():
    """A follower cancelling must not destroy the leader's shared future."""

    async def scenario():
        dedup = InFlightDeduplicator()

        async def factory():
            await asyncio.sleep(0.05)
            return "done"

        leader = asyncio.create_task(dedup.run("fp", factory))
        await asyncio.sleep(0)  # let leader register
        follower = asyncio.create_task(dedup.run("fp", factory))
        await asyncio.sleep(0.01)
        follower.cancel()
        with pytest.raises(asyncio.CancelledError):
            await follower
        return await leader

    result = asyncio.run(scenario())
    assert result.value == "done"


def test_6_entry_is_released_after_completion_no_cross_turn_cache():
    """A4/Test 10: equivalent SQL must NOT be reused on a later turn."""

    async def scenario():
        dedup = InFlightDeduplicator()
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            return calls

        await dedup.run("fp", factory)          # turn 1
        assert dedup.in_flight() == 0           # key dropped
        await dedup.run("fp", factory)          # turn 2 — must re-execute
        return calls

    assert asyncio.run(scenario()) == 2


# ── Fingerprint scoping (Test 10: no sharing across authz contexts) ──────


def test_10_fingerprint_scoped_by_tenant_and_source():
    args = {"question": "last week sales"}
    same = request_fingerprint(
        "query_database", args, org_id="org-a", conversation_id="c1",
        bound_kb_ids=["kb1"],
    )
    assert same == request_fingerprint(
        "query_database", args, org_id="org-a", conversation_id="c1",
        bound_kb_ids=["kb1"],
    )
    # Different org → different fingerprint (never share rows across tenants).
    assert same != request_fingerprint(
        "query_database", args, org_id="org-b", conversation_id="c1",
        bound_kb_ids=["kb1"],
    )
    # Different datasource scope → different fingerprint.
    assert same != request_fingerprint(
        "query_database", args, org_id="org-a", conversation_id="c1",
        bound_kb_ids=["kb2"],
    )


def test_10_sql_fingerprint_normalizes_whitespace_and_case():
    a = sql_fingerprint("SELECT  SUM(FAMOUNT)\n  FROM erp_v_sale")
    b = sql_fingerprint("select sum(famount) from erp_v_sale")
    assert a == b
    assert sql_fingerprint(None) is None
    assert sql_fingerprint("   ") is None


def test_10_disabled_dedup_runs_directly():
    async def scenario():
        dedup = InFlightDeduplicator()
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            return calls

        r1 = await dedup.run("fp", factory, enabled=False)
        r2 = await dedup.run("fp", factory, enabled=False)
        return calls, r1.shared, r2.shared

    calls, s1, s2 = asyncio.run(scenario())
    assert calls == 2 and s1 is False and s2 is False


def test_contextvar_scope_roundtrip():
    d = InFlightDeduplicator()
    token = set_inflight_dedup(d)
    assert get_inflight_dedup() is d
    reset_inflight_dedup(token)
    assert get_inflight_dedup() is None


# ── Test 9: a UI/plan-step failure must not erase a valid result ─────────


def test_9_plan_summary_failure_does_not_erase_assembled_rows():
    """The plan checklist can fail while the data is perfectly good.

    The contract classifies from the OBSERVATIONS first; plan-node errors
    only explain a failure when nothing was executed. A successful
    observation must therefore win over a failed plan node.
    """
    from app.services.db.result_contract import (
        ExecutionStatus,
        ResultStatus,
        normalize_query_result,
    )

    obs = SimpleNamespace(
        seq=0,
        observation_type="nl2sql",
        success=True,
        error_message=None,
        result_data={"sql": "SELECT 1 AS x", "data": [{"x": 1}, {"x": 2}]},
    )
    db = SimpleNamespace()  # not consulted: the object carries observations
    result = SimpleNamespace(
        execution_id="e1",
        observations=[obs],
        # A failed plan node — the UI checklist shows an incomplete step.
        plan_summary={
            "nodes": [{"seq": 9, "status": "failed", "error": "UI render failed"}]
        },
    )

    normalized = normalize_query_result(result, db, question="q")
    assert normalized.success is True
    assert normalized.execution_status is ExecutionStatus.SUCCEEDED
    assert normalized.result_status is ResultStatus.ASSEMBLED
    assert normalized.row_count == 2, "the valid rows must be preserved"


# ── Test 10: ask_data_agent and query_database agree ─────────────────────


def test_10_both_tools_share_the_canonical_contract():
    """Both data tools must expose the same result-contract keys so the agent
    cannot interpret one execution two different ways."""
    from app.services.db.result_contract import (
        ExecutionStatus,
        NormalizedQueryResult,
        ResultStatus,
    )

    n = NormalizedQueryResult(
        execution_status=ExecutionStatus.SUCCEEDED,
        result_status=ResultStatus.ASSEMBLED,
        rows=[{"a": 1}],
        source_id="kb1",
        source_name="wh",
        provenance={"execution_id": "e1"},
    )
    tool = n.to_tool_result()

    for key in (
        "success",
        "rows",
        "row_count",
        "complete",
        "execution_status",
        "result_status",
        "failure_kind",
        "provenance",
    ):
        assert key in tool, f"contract key missing: {key}"

    assert tool["success"] is True
    assert tool["row_count"] == 1
    assert tool["execution_status"] == "succeeded"
    assert tool["result_status"] == "assembled"
    assert tool["failure_kind"] == "none"
    assert tool["provenance"]["execution_id"] == "e1"


def test_10_zero_row_success_agrees_across_contract():
    from app.services.db.result_contract import (
        ExecutionStatus,
        NormalizedQueryResult,
        ResultStatus,
    )

    n = NormalizedQueryResult(
        execution_status=ExecutionStatus.SUCCEEDED,
        result_status=ResultStatus.EMPTY,
    )
    tool = n.to_tool_result()
    assert tool["success"] is True
    assert tool["row_count"] == 0
    assert tool["result_status"] == "empty"
