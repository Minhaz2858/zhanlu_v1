"""Cycle-2 remediation tests (2026-10-29).

Basis: the authorized controlled replay of 2026-10-29 —
- fresh FSM executions reset the per-turn SQL budget (17 answer() runs, 8
  successful, budget of 6 never tripped);
- six UNION-shaped candidates were rejected as "Only SELECT statements are
  allowed, got: union" (a read-only construct) and each rejection burned a
  full regeneration cycle;
- a guard discarded a complete 5,042-char grounded report and an 89-char
  re-synthesis fragment replaced it on stream, terminal frame and
  persistence alike.

Covers: turn-wide budget sharing/charging/atomicity/isolation, dialect
support (UNION on MySQL and PostgreSQL shapes), and the answer-recovery
floor that makes the 5,131-vs-89 regression shape impossible.
"""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlglot

from app.services.answer_recovery import (
    accept_resynthesis,
    final_answer,
    register_best,
    strip_trailing_pending,
)
from app.services.db import turn_sql_budget
from app.services.db.execution_gate import enforce
from app.services.db.query_contract import validate_confusable_measures
from app.services.nl2sql.validator import validate as validate_governance
from app.services.synexia.replan_policy import is_non_actionable_error


# ── Phase 1: turn-wide SQL budget ───────────────────────────────────────────


def _acquire(scope, n, base=6):
    got = 0
    for _ in range(n):
        ok, used, eff = turn_sql_budget.try_acquire(scope, base, 1800.0)
        got += 1 if ok else 0
    return got, used, eff


def test_turn_budget_shared_across_nested_executions():
    """Multiple FSM executions inside ONE turn share a single budget —
    the replay's exact gap (each execution got a fresh exec-scoped one)."""
    turn_sql_budget.reset_scope("turn:c2-share")
    # Three nested "executions" each acquire 2 attempts — all under the
    # SAME canonical turn scope.
    got1, _, _ = _acquire("turn:c2-share", 2)
    got2, used, _ = _acquire("turn:c2-share", 2)
    got3, used, eff = _acquire("turn:c2-share", 2)
    assert (got1, got2, got3) == (2, 2, 2)
    assert used == 6 and eff == 6
    # The 7th attempt of the SAME turn is refused — no fresh budget.
    ok, used, _ = turn_sql_budget.try_acquire("turn:c2-share", 6, 1800.0)
    assert not ok and used == 6
    turn_sql_budget.reset_scope("turn:c2-share")


def test_failed_and_rejected_attempts_are_charged():
    """try_acquire charges UPFRONT — there is no release path, so a
    gate-rejected or failed attempt consumes budget exactly like a
    successful execution."""
    turn_sql_budget.reset_scope("turn:c2-fail")
    ok1, _, _ = turn_sql_budget.try_acquire("turn:c2-fail", 2, 1800.0)
    # Imagine this attempt's SQL got rejected — no refund exists:
    ok2, used, _ = turn_sql_budget.try_acquire("turn:c2-fail", 2, 1800.0)
    assert ok1 and ok2 and used == 2
    ok3, used, _ = turn_sql_budget.try_acquire("turn:c2-fail", 2, 1800.0)
    assert not ok3 and used == 2
    turn_sql_budget.reset_scope("turn:c2-fail")


def test_turn_isolation():
    """Independent turns have independent budgets (no cross-turn bleed)."""
    turn_sql_budget.reset_scope("turn:c2-a")
    turn_sql_budget.reset_scope("turn:c2-b")
    got_a, _, _ = _acquire("turn:c2-a", 6)
    ok_b, used_b, _ = turn_sql_budget.try_acquire("turn:c2-b", 6, 1800.0)
    assert got_a == 6
    assert ok_b and used_b == 1
    turn_sql_budget.reset_scope("turn:c2-a")
    turn_sql_budget.reset_scope("turn:c2-b")


def test_concurrent_acquires_are_atomic():
    """Concurrent tool calls in one turn cannot both slip in at budget-1
    (check-and-increment is one locked critical section)."""
    turn_sql_budget.reset_scope("turn:c2-conc")
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(
            lambda _: turn_sql_budget.try_acquire("turn:c2-conc", 6, 1800.0)[0],
            range(10),
        ))
    assert sum(results) == 6  # exactly the budget, no double-spend
    turn_sql_budget.reset_scope("turn:c2-conc")


def test_execution_request_carries_turn_identity():
    """The canonical turn identity rides on ExecutionRequest (explicit
    propagation requirement) — and a retry reuses the caller's identity
    while a different turn gets a different one."""
    from app.services.synexia.fsm import ExecutionRequest

    req = ExecutionRequest(
        conversation_id="c", agent_name="general_assistant",
        user_message="q", turn_id="turn:abc",
    )
    assert req.turn_id == "turn:abc"
    # A retry reuses the same identity (caller decides); an independent
    # turn naturally produces a different one.
    retry = ExecutionRequest(
        conversation_id="c", agent_name="general_assistant",
        user_message="q", turn_id=req.turn_id,
    )
    other = ExecutionRequest(
        conversation_id="c", agent_name="general_assistant",
        user_message="q", turn_id=str(__import__("uuid").uuid4()),
    )
    assert retry.turn_id == req.turn_id
    assert other.turn_id != req.turn_id


def test_data_agent_budget_refusal_is_non_replannable():
    """A budget refusal must not trigger another regeneration round —
    the honest refusal is terminal for the node."""
    assert is_non_actionable_error(
        "This turn has already spent 6 of 6 allowed SQL attempts against "
        "the data source."
    )
    assert is_non_actionable_error(
        "Only SELECT statements are allowed, got: insert"
    )


# ── Phase 2: dialect support (read-only set operations) ─────────────────────


def test_union_accepted_as_read_only_sql():
    """UNION of SELECTs is read-only, valid in every supported dialect —
    rejecting it burned six regeneration cycles in the replay."""
    for label, sql in (
        ("mysql-ish", "SELECT plan_date, SUM(fline_amount) AS r FROM order_line GROUP BY plan_date UNION SELECT plan_date, SUM(fgross_amount) AS g FROM order_line GROUP BY plan_date"),
        ("postgres-ish", "SELECT region FROM sales_fact EXCEPT SELECT region FROM sales_fact WHERE revenue_net > 0"),
    ):
        result = validate_governance(sql)
        assert not result.errors, f"{label}: {result.errors}"


@pytest.mark.parametrize("sql,fragment", [
    ("DROP TABLE order_line", "Only SELECT statements"),
    ("DELETE FROM order_line WHERE 1=1", "Blocked statement type"),
    ("INSERT INTO order_line VALUES (1)", "Blocked statement type"),
    ("SELECT 1; DROP TABLE order_line", "Multi-statement"),
])
def test_genuinely_unsupported_sql_still_rejected(sql, fragment):
    result = validate_governance(sql)
    assert result.errors and any(fragment in e for e in result.errors)


# The UNION-through-the-gate test lives in test_query_contract.py (it needs
# the two_datasources fixture defined there): test_union_passes_metric_binding_gate_with_both_bases.


# ── Phase 3: final-answer recovery (5,131 vs 89 regression shape) ──────────


def _report_like_text(n=5042):
    body = ("Weekly sales report. " * 220)[:n]
    return body + "\n\nWant this as a formatted deck or a one-page PDF for distribution?"


def test_fragment_cannot_replace_full_report():
    """THE replay regression, as a decision: an 89-char intent sentence
    produced by an emergency re-synthesis must NOT replace the 5,042-char
    grounded report."""
    report = _report_like_text(5042)
    fragment = "I'll pull last week's sales data (Mon 19 – Sun 25 Oct 2026) with a prior-week comparison."
    assert len(fragment) == 89
    assert not accept_resynthesis(fragment, report)
    resolved = final_answer(fragment, report)
    assert len(resolved) > 4000
    assert "Weekly sales report." in resolved
    # The trailing pending-work offer that triggered the original discard
    # is deterministically stripped from the recovered report.
    assert "Want this as a formatted deck" not in resolved


def test_comparable_resynthesis_is_accepted():
    """A genuine full re-synthesis still replaces the preserved text."""
    best = _report_like_text(3000)
    good = "Complete rewritten report with all sections and figures. " * 45
    assert accept_resynthesis(good, best)
    assert final_answer(good, best) == good


def test_recovery_without_preserved_best_uses_plain_floor():
    assert accept_resynthesis("x" * 89, "")
    assert not accept_resynthesis("x" * 49, "")


def test_register_best_tolerates_none_initialized_slot():
    """The generator initializes recovery slots as [None] — registration
    must treat that as empty, never raise (live TypeError seen 02:59:30
    in the 2026-10-30 backend log before this fix)."""
    store = [None]  # type: ignore[list-item]
    report = _report_like_text(5042)
    register_best(store, report)
    assert store[0] == report
    register_best(store, "x" * 89)
    assert store[0] == report  # fragment did not regress it
    # Also safe when handed empty text or an empty list.
    register_best(store, "")
    register_best([], "seed")
    assert store == [report]


def test_register_best_is_grow_only():
    store = []
    register_best(store, _report_like_text(5042))
    register_best(store, "x" * 89)      # fragment must not regress it
    register_best(store, "x" * 6000)    # longer text wins
    register_best(store, "")            # empty never clears it
    assert len(store) == 1 and len(store[0]) == 6000


def test_strip_trailing_pending_keeps_body():
    text = _report_like_text(1000)
    stripped = strip_trailing_pending(text)
    assert stripped.startswith("Weekly sales report.")
    assert "Want this as a formatted deck" not in stripped
    # A text without a trailing offer is returned untouched.
    plain = "Report body without any offer sentence. " * 20
    assert strip_trailing_pending(plain) == plain.rstrip() or strip_trailing_pending(plain) == plain


def test_recovery_module_is_content_agnostic():
    """No datasource/schema/column knowledge may leak into the recovery
    rules — they are pure text-shape decisions."""
    import inspect
    from app.services import answer_recovery

    src = inspect.getsource(answer_recovery)
    for banned in ("FAMOUNT", "FALLAMOUNT", "erp_", "order_line", "sales_fact"):
        assert banned not in src
