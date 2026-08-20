"""Announcement-scoped pending-action tracking — sequence-stamp semantics.

Regression: the original turn-scoped _announced_executed flip caused the
promise-as-final-answer bug. Two OK queries ran (executed_seq = 2), then the
exit branch armed a promise from the model's final prose (armed_seq = 3),
but _unmet_pending_action returned [] because the stale flip was still True
from the earlier query (3 > 2 was never checked).

Fix: replace _announced_executed / _last_query_quality with _seq /
_armed_seq / _executed_seq / _armed_by / _usable_results. A pending phrase
fires iff _armed_seq > _executed_seq. When usable data exists
(_usable_results > 0), force_synthesis=True (answer with existing data)
instead of force_tool (wasteful re-query).
"""

from __future__ import annotations

import pytest

from app.services.goal_contract import (
    RESULT_QUALITY_ASSUMED_OK,
    RESULT_QUALITY_NO_DATA,
    GoalContract,
    build_goal_contract,
    pending_action_phrase,
)


# ── the exact bug: two OK queries → exit promise → must force synthesis ────


def test_two_ok_queries_then_exit_promise_fires_force_synthesis() -> None:
    """Exact reproduction of the user's turn shape."""
    c = build_goal_contract("Give me supply chain data for last 30 days")
    # query #1: stale table, but usable rows
    c.record_tool_executed("ask_data_agent", result_quality=RESULT_QUALITY_ASSUMED_OK)
    c.record_query_result([{"product_name": "C5", "revenue": 1000}])
    # query #2: live table, usable rows
    c.record_tool_executed("ask_data_agent", result_quality=RESULT_QUALITY_ASSUMED_OK)
    c.record_query_result([{"product_name": "C9", "revenue": 5000}])
    # exit branch: model writes a promise as the final answer
    c.refresh_pending_action(
        "The first attempt used the stale table. Let me re-query against "
        "the live, current tables that cover through 2026."
    )
    crits = c.unmet(granted_tools={"ask_data_agent"})
    assert crits, "pending-action remediation MUST fire (this is the bug)"
    assert crits[0].code == "pending_action"
    assert crits[0].force_synthesis is True, "data already retrieved — force synthesis, not re-query"
    assert crits[0].force_tool is None


# ── same-phrase no re-stamp ──────────────────────────────────────────────


def test_refresh_same_phrase_does_not_restamp() -> None:
    c = build_goal_contract("show me sales data")
    c.refresh_pending_action("Let me query the sales data now.")
    seq_after_first = c._armed_seq
    c.refresh_pending_action("Let me query the sales data now.")
    assert c._armed_seq == seq_after_first, "same phrase must not re-stamp"


# ── NO_DATA quality does not stamp _executed_seq ─────────────────────────


def test_no_data_does_not_stamp_execution() -> None:
    c = build_goal_contract("show me sales")
    c.pending_action_phrase = "let me query now"
    c._armed_seq = 1
    c._seq = 1
    c.record_tool_executed("execute_query", result_quality=RESULT_QUALITY_NO_DATA)
    assert c._executed_seq == 0, "NO_DATA must not stamp _executed_seq"
    crits = c.unmet(granted_tools={"execute_query"})
    assert any(cr.code == "pending_action" for cr in crits)


# ── exec-after-arm satisfies the promise ──────────────────────────────────


def test_execution_after_armed_satisfies() -> None:
    c = build_goal_contract("show me sales")
    c.refresh_pending_action("Let me query the sales table.")
    assert c._armed_seq > 0
    # Now a real query executes AFTER the promise
    c.record_tool_executed("execute_query", result_quality=RESULT_QUALITY_ASSUMED_OK)
    assert c._executed_seq >= c._armed_seq
    assert c.unmet(granted_tools={"execute_query"}) == []


# ── _PENDING_OFFER_RE: non-action offers must not force ───────────────────


def test_offer_let_me_know_not_forced() -> None:
    """Closing offer 'Let me know if…' is not an action promise."""
    c = build_goal_contract("show me sales data")
    c.record_tool_executed("execute_query", result_quality=RESULT_QUALITY_ASSUMED_OK)
    c.record_query_result([{"product": "X", "qty": 10}])
    c.refresh_pending_action("Here is the sales data. Let me know if you'd like more detail.")
    assert c.pending_action_phrase is None, "offer must be excluded by _PENDING_OFFER_RE"


def test_offer_feel_free_not_forced() -> None:
    c = build_goal_contract("show me sales")
    c.record_tool_executed("execute_query", result_quality=RESULT_QUALITY_ASSUMED_OK)
    c.record_query_result([{"p": "A"}])
    c.refresh_pending_action("Feel free to ask if you need a breakdown.")
    assert c.pending_action_phrase is None


# ── user-armed never disarmed by clean prose ─────────────────────────────


def test_user_armed_not_disarmed_by_clean_prose() -> None:
    """When build_goal_contract arms the phrase from the user's text (e.g.
    'I'd like to see sales'), the model's clean prose must NOT disarm it —
    only execution satisfies a user-armed promise."""
    c = build_goal_contract("I'd like to see sales data for Q3")
    assert c.pending_action_phrase is not None
    assert c._armed_by == "user"
    # Model gives a clean answer without executing
    c.refresh_pending_action("Here's what I can tell you about the general market trends.")
    # User-armed phrase must still be armed
    assert c.pending_action_phrase is not None, "user-armed must NOT be disarmed by prose"
    crits = c.unmet(granted_tools={"execute_query"})
    assert any(cr.code == "pending_action" for cr in crits)


# ── model-armed disarmed by clean final prose ────────────────────────────


def test_model_armed_disarmed_by_clean_prose() -> None:
    """After forced synthesis, if the model delivers a clean answer (no
    pending phrase), the model-armed marker is disarmed — preventing force
    loops."""
    c = build_goal_contract("show me sales data")
    c.record_tool_executed("execute_query", result_quality=RESULT_QUALITY_ASSUMED_OK)
    c.refresh_pending_action("Let me re-query against the live tables.")
    assert c._armed_by == "model"
    # Model delivers a clean answer after forced synthesis
    c.refresh_pending_action("The total sales revenue for the last 30 days is $1.2M.")
    assert c.pending_action_phrase is None, "model-armed must be disarmed by clean prose"
    assert c.unmet(granted_tools={"execute_query"}) == []


# ── synthesis vs query force selection ────────────────────────────────────


def test_force_synthesis_when_usable_results_exist() -> None:
    c = build_goal_contract("show me shipments")
    c.record_tool_executed("ask_data_agent", result_quality=RESULT_QUALITY_ASSUMED_OK)
    c.record_query_result([{"product": "C5", "qty": 500}])
    c.refresh_pending_action("Let me pull the shipment data now.")
    crits = c.unmet(granted_tools={"ask_data_agent"})
    assert crits[0].force_synthesis is True
    assert crits[0].force_tool is None


def test_force_query_when_no_usable_results() -> None:
    c = build_goal_contract("show me shipments")
    # No usable results yet
    c.refresh_pending_action("Let me query the shipments table.")
    crits = c.unmet(granted_tools={"execute_query"})
    assert crits[0].force_synthesis is False
    assert crits[0].force_tool == "execute_query"


# ── budget exhaustion ────────────────────────────────────────────────────


def test_budget_exhausted_returns_empty() -> None:
    c = build_goal_contract("show me shipments")
    c.max_forces = 1
    c.record_force()
    c.refresh_pending_action("Let me re-query the data.")
    assert c.unmet(granted_tools={"execute_query"}) == []


# ── pending_action_phrase with offer exclusion ───────────────────────────


def test_pending_action_phrase_excludes_let_me_know() -> None:
    result = pending_action_phrase("Here is the data. Let me know if you need more.")
    assert result is None


def test_pending_action_phrase_excludes_feel_free() -> None:
    result = pending_action_phrase("Feel free to ask for more details.")
    assert result is None


def test_pending_action_phrase_keeps_action_promise() -> None:
    result = pending_action_phrase("Let me query the live sales table now.")
    assert result is not None
    assert "query" in result.lower()


# ── D3 (2026-08-20): exact user-trace sentence "let me re-run a single
# ── clean aggregation" must be detected as a pending action ────────────────


def test_pending_action_phrase_keeps_rerun_aggregation_promise() -> None:
    """Exact trace sentence: matches _PENDING_RE via 'let me', is NOT excluded
    by _PENDING_OFFER_RE ('let me know'/'feel free'/'want me to') nor by
    _PENDING_PAST_RE ('ran' ≠ 're-run'), so pending_action_phrase() MUST
    return the sentence."""
    result = pending_action_phrase(
        "Let me re-run a single clean aggregation to confirm the totals are "
        "internally consistent across all metrics."
    )
    assert result is not None, "trace sentence must be detected as pending action"
    assert "re-run" in result.lower()


def test_two_ok_queries_then_rerun_aggregation_promise_fires() -> None:
    """Full turn-shape repro: two OK queries already executed, then the exit
    branch arms a promise with the EXACT trace sentence. The sequence-stamp
    check (_armed_seq > _executed_seq) MUST fire remediation with
    force_synthesis=True (usable data already retrieved)."""
    c = build_goal_contract("Give me supply chain data for last 30 days")
    c.record_tool_executed("ask_data_agent", result_quality=RESULT_QUALITY_ASSUMED_OK)
    c.record_query_result([{"product_name": "C5", "revenue": 1000}])
    c.record_tool_executed("ask_data_agent", result_quality=RESULT_QUALITY_ASSUMED_OK)
    c.record_query_result([{"product_name": "C9", "revenue": 5000}])
    c.refresh_pending_action(
        "Let me re-run a single clean aggregation to confirm the totals are "
        "internally consistent across all metrics."
    )
    crits = c.unmet(granted_tools={"ask_data_agent"})
    assert crits, "pending-action remediation MUST fire (this is the D3 repro)"
    assert crits[0].code == "pending_action"
    assert crits[0].force_synthesis is True, "data already retrieved — force synthesis"
    assert crits[0].force_tool is None


# ── _PENDING_OFFER_RE: "want me to" offers must not force ──────────────────


def test_pending_action_phrase_excludes_want_me_to() -> None:
    """'Let me … if you want me to' is a conditional OFFER (matched by both
    _PENDING_RE and the expanded _PENDING_OFFER_RE), so it must be excluded."""
    result = pending_action_phrase(
        "Here is the summary. Let me re-query the live tables if you want me to."
    )
    assert result is None, "'want me to' offer must be excluded by _PENDING_OFFER_RE"
