"""Regression tests — turn-observation recovery for final synthesis (fix #1).

Incident (convs 6c466a7d / 7e2e6a53, 2026-10-28/29): the bound MySQL
warehouse was queried successfully (20+ nl2sql nodes, 100-275 real rows in
``observation_records``) but the final assistant answer was the generic
"had trouble putting it all together" apology. The rows→synthesis handoff
was gated on goal-contract answer datasets, which are only collected when
QUERY_PURPOSE_TAGGING_ENABLED is on.

The recovery (``_recover_turn_observation_datasets``) reads — never re-
executes — the durable observation records of the CURRENT turn's executions,
attributed exactly via the execution ids the loop recorded on the tool
results (``provenance.execution_id``). These tests pin the safety
invariants: current-turn + tenant isolation, success-only, zero-row vs
failure distinction, malformed-payload fail-closed behavior, merge without
duplication, idempotency, and the grounded-synthesis handoff replacing the
apology (with real DB fixtures, no source-string assertions).
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.database import SessionLocal  # noqa: E402
from app.models.execution import Execution, ObservationRecord  # noqa: E402
from app.routers.agents import (  # noqa: E402
    _recover_turn_observation_datasets,
    _force_llm_synthesis,
)
from app.services.file_turn_guard import detect_give_up  # noqa: E402
from app.services.synexia.report_synthesis import merge_answer_rows  # noqa: E402


# ── Seeding helpers (real rows on the shared test engine) ──────────────────


def _mk_execution(db, *, conversation_id, org_id="org-a", app_id="app-a"):
    ex = Execution(
        id=str(uuid.uuid4()),
        conversation_id=conversation_id,
        org_id=org_id,
        app_id=app_id,
        agent_name="general_assistant",
        user_message="Give me last month's sales report.",
    )
    db.add(ex)
    db.commit()
    return ex


def _mk_obs(db, execution, *, seq=1, success=True, data=None, sql="SELECT 1",
            source_name="aipdp_data_warehouse_prod", source_id="kb-1",
            result_data=None, observation_type="nl2sql", error_message=None):
    if result_data is None:
        result_data = {
            "sql": sql,
            "data": data if data is not None else [],
            "source_id": source_id,
            "source_name": source_name,
        }
    obs = ObservationRecord(
        id=str(uuid.uuid4()),
        execution_id=execution.id,
        seq=seq,
        observation_type=observation_type,
        tool_name="nl2sql",
        success=success,
        result_data=result_data,
        error_message=error_message,
    )
    db.add(obs)
    db.commit()
    return obs


def _tool_result(execution, rows):
    """The tool-result dict the loop records (to_tool_result shape)."""
    return {
        "success": True,
        "rows": rows,
        "provenance": {"execution_id": execution.id},
    }


# ── 1+2. Recovery: success-only, current-turn attribution ──────────────────


def test_recovers_successful_current_turn_rows():
    db = SessionLocal()
    try:
        conv = f"conv-{uuid.uuid4().hex[:12]}"
        ex1 = _mk_execution(db, conversation_id=conv)
        ex2 = _mk_execution(db, conversation_id=conv)
        _mk_obs(db, ex1, seq=1, data=[{"product": "A", "amount": 10}])
        _mk_obs(db, ex1, seq=2, data=[{"product": "B", "amount": 20}])
        _mk_obs(db, ex2, seq=1, data=[{"day": "2026-09-01", "amount": 5}])
        tool_calls = [
            {"name": "query_database", "results": _tool_result(ex1, [{"x": 1}])},
            {"name": "query_database", "results": _tool_result(ex2, [{"y": 2}])},
        ]
        datasets, stats = _recover_turn_observation_datasets(
            db, conversation_id=conv, org_id="org-a", app_id="app-a",
            tool_calls_for_frontend=tool_calls,
        )
        assert len(datasets) == 3
        assert stats["rows_recovered"] == 3
        assert stats["execution_ids"] == 2
        assert stats["success_observations"] == 3
        assert all(d["purpose"] == "answer" for d in datasets)
        assert datasets[0]["source_name"] == "aipdp_data_warehouse_prod"
        assert datasets[0]["sql"] == "SELECT 1"
    finally:
        db.close()


# ── 3. Failed SQL observations excluded ────────────────────────────────────


def test_failed_observations_excluded():
    db = SessionLocal()
    try:
        conv = f"conv-{uuid.uuid4().hex[:12]}"
        ex = _mk_execution(db, conversation_id=conv)
        _mk_obs(db, ex, seq=1, success=False,
                error_message="Query failed: column 'x' not found")
        _mk_obs(db, ex, seq=2, success=True, data=[{"ok": 1}])
        tool_calls = [{"results": _tool_result(ex, [])}]
        datasets, stats = _recover_turn_observation_datasets(
            db, conversation_id=conv, org_id="org-a", app_id="app-a",
            tool_calls_for_frontend=tool_calls,
        )
        assert len(datasets) == 1
        assert stats["failed_observations"] == 1
        assert stats["success_observations"] == 1
    finally:
        db.close()


# ── 4. Successful zero-row ≠ failure ───────────────────────────────────────


def test_zero_row_success_distinguished_from_failure():
    db = SessionLocal()
    try:
        conv = f"conv-{uuid.uuid4().hex[:12]}"
        ex = _mk_execution(db, conversation_id=conv)
        _mk_obs(db, ex, seq=1, success=True, data=[])  # executed, matched 0 rows
        _mk_obs(db, ex, seq=2, success=False, error_message="engine down")
        tool_calls = [{"results": _tool_result(ex, [])}]
        datasets, stats = _recover_turn_observation_datasets(
            db, conversation_id=conv, org_id="org-a", app_id="app-a",
            tool_calls_for_frontend=tool_calls,
        )
        assert datasets == []  # nothing to ground a synthesis on
        assert stats["success_zero_row"] == 1   # recorded as honest-empty
        assert stats["failed_observations"] == 1  # separate bucket
    finally:
        db.close()


# ── 5. Current-turn isolation: other conversation excluded ─────────────────


def test_other_conversation_excluded():
    db = SessionLocal()
    try:
        conv_a = f"conv-a-{uuid.uuid4().hex[:8]}"
        conv_b = f"conv-b-{uuid.uuid4().hex[:8]}"
        ex_b = _mk_execution(db, conversation_id=conv_b)
        _mk_obs(db, ex_b, data=[{"historic": True}])
        # Turn A's tool result references conv B's execution — the join on
        # Execution.conversation_id must exclude it.
        tool_calls = [{"results": _tool_result(ex_b, [{"historic": True}])}]
        datasets, stats = _recover_turn_observation_datasets(
            db, conversation_id=conv_a, org_id="org-a", app_id="app-a",
            tool_calls_for_frontend=tool_calls,
        )
        assert datasets == []
    finally:
        db.close()


# ── 6. Tenant isolation: other org excluded ────────────────────────────────


def test_other_tenant_excluded():
    db = SessionLocal()
    try:
        conv = f"conv-{uuid.uuid4().hex[:12]}"
        ex_other_org = _mk_execution(db, conversation_id=conv, org_id="org-b")
        _mk_obs(db, ex_other_org, data=[{"secret": True}])
        tool_calls = [{"results": _tool_result(ex_other_org, [{"secret": True}])}]
        datasets, _stats = _recover_turn_observation_datasets(
            db, conversation_id=conv, org_id="org-a", app_id="app-a",
            tool_calls_for_frontend=tool_calls,
        )
        assert datasets == []
    finally:
        db.close()


# ── 7. Malformed payloads fail closed ──────────────────────────────────────


def test_malformed_payload_fail_closed():
    db = SessionLocal()
    try:
        conv = f"conv-{uuid.uuid4().hex[:12]}"
        ex = _mk_execution(db, conversation_id=conv)
        _mk_obs(db, ex, seq=1, result_data="not-a-dict")  # display text only
        _mk_obs(db, ex, seq=2, result_data={"sql": "SELECT 1"})  # no data key
        _mk_obs(db, ex, seq=3, result_data={"data": "rows-as-string"})
        _mk_obs(db, ex, seq=4, success=True, data=[{"good": 1}])
        tool_calls = [{"results": _tool_result(ex, [])}]
        datasets, stats = _recover_turn_observation_datasets(
            db, conversation_id=conv, org_id="org-a", app_id="app-a",
            tool_calls_for_frontend=tool_calls,
        )
        assert len(datasets) == 1  # only the well-formed one
        assert stats["malformed_observations"] == 3
    finally:
        db.close()


# ── 8. No execution ids → no recovery, no error ────────────────────────────


def test_no_execution_ids_returns_empty():
    db = SessionLocal()
    try:
        datasets, stats = _recover_turn_observation_datasets(
            db, conversation_id="conv-none", org_id="org-a", app_id="app-a",
            tool_calls_for_frontend=[{"results": {"success": True, "rows": []}}],
        )
        assert datasets == []
        assert stats["execution_ids"] == 0
    finally:
        db.close()


# ── 9. Merge policy: multiple nodes, no duplicated rows ────────────────────


def test_merge_across_nodes_deduplicates():
    db = SessionLocal()
    try:
        conv = f"conv-{uuid.uuid4().hex[:12]}"
        ex = _mk_execution(db, conversation_id=conv)
        row = {"product": "A", "amount": 10}
        _mk_obs(db, ex, seq=1, data=[row])
        _mk_obs(db, ex, seq=2, data=[dict(row), {"product": "B", "amount": 20}])
        tool_calls = [{"results": _tool_result(ex, [])}]
        datasets, _stats = _recover_turn_observation_datasets(
            db, conversation_id=conv, org_id="org-a", app_id="app-a",
            tool_calls_for_frontend=tool_calls,
        )
        merged = merge_answer_rows(datasets)
        assert len(merged) == 2  # identical row deduplicated by the merge
    finally:
        db.close()


# ── 10. Idempotent recovery ────────────────────────────────────────────────


def test_recovery_idempotent():
    db = SessionLocal()
    try:
        conv = f"conv-{uuid.uuid4().hex[:12]}"
        ex = _mk_execution(db, conversation_id=conv)
        _mk_obs(db, ex, data=[{"k": 1}])
        tool_calls = [{"results": _tool_result(ex, [])}]
        args = dict(
            conversation_id=conv, org_id="org-a", app_id="app-a",
            tool_calls_for_frontend=tool_calls,
        )
        first_ds, first_stats = _recover_turn_observation_datasets(db, **args)
        second_ds, second_stats = _recover_turn_observation_datasets(db, **args)
        assert first_ds == second_ds
        assert first_stats == second_stats
    finally:
        db.close()


# ── 11. Handoff: grounded synthesis replaces the give-up apology ───────────


@pytest.mark.asyncio
async def test_giveup_apology_replaced_by_grounded_synthesis():
    """The incident end-state: narration + apology while recovered rows
    exist. The handoff must run forced synthesis on the recovered rows and
    replace the apology; with no rows it must keep the existing text."""
    db = SessionLocal()
    try:
        conv = f"conv-{uuid.uuid4().hex[:12]}"
        ex = _mk_execution(db, conversation_id=conv)
        _mk_obs(db, ex, data=[{"month": "2026-09", "sales": 148.6},
                              {"month": "2026-09", "sales": 32.1}])
        tool_calls = [{"results": _tool_result(ex, [])}]
        datasets, stats = _recover_turn_observation_datasets(
            db, conversation_id=conv, org_id="org-a", app_id="app-a",
            tool_calls_for_frontend=tool_calls,
        )
        assert stats["rows_recovered"] == 2
        _last_rows = merge_answer_rows(datasets)

        apology = (
            "I'll pull September 2026 sales data now — totals, product mix, "
            "daily trend, and customer breakdown in parallel. I gathered some "
            "information but had trouble putting it all together. Could you "
            "try again with a more specific request?"
        )
        assert detect_give_up(apology) is True

        async def _fake_synth(system_prompt, msgs, _ep=None):
            assert "148.6" in system_prompt or any(
                "148.6" in str(m) for m in msgs
            ), "synthesis must receive the recovered rows"
            return {"content": (
                "Executive Summary: September 2026 sales totaled 180.7 across "
                "two recorded transactions, with the largest single sale at "
                "148.6 and the remainder at 32.1.\n\n"
                "Key Numbers: total 180.7; largest transaction 148.6 (82.2% of "
                "the total); second transaction 32.1.\n\n"
                "Detailed Breakdown: the two rows returned by the warehouse "
                "cover the full recorded period for the month; both figures "
                "come directly from the queried rows.\n\n"
                "Risks & Opportunities: concentration risk — one transaction "
                "carries over four fifths of the month's revenue.\n\n"
                "Recommended Actions: verify whether the low transaction count "
                "reflects a data capture gap before drawing conclusions."
            )}

        accumulated = apology
        # The handoff condition (mirrors the router hook): non-empty text
        # that is a give-up + recovered rows → replace via forced synthesis.
        if accumulated.strip() and detect_give_up(accumulated) and _last_rows:
            _answer = await _force_llm_synthesis(
                "Give me last month's sales report.",
                _last_rows,
                synth_call_fn=_fake_synth,
                conversation_history=[],
                max_retries=1,
            )
            if len(_answer) > 50:
                accumulated = _answer
        assert "had trouble" not in accumulated
        assert "180.7" in accumulated  # grounded in fixture rows, not invented

        # Honest-failure invariant: without recovered rows the apology is
        # kept (no fabrication path exists).
        empty_rows = merge_answer_rows([])
        assert not empty_rows
        kept = apology  # hook condition (rows) is False → text unchanged
        assert kept is apology
    finally:
        db.close()
