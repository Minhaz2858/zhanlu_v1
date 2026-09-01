"""Promise-as-answer at soft-cap budget check (Fix 3b).

Root cause: when the model produces promise text ("Let me verify…") alongside
tool calls in the same LLM response, `refresh_pending_action` is only called
in the exit branch (which requires `raw_tool_calls` to be empty). Tool
execution after arming advances `_executed_seq` past `_armed_seq`, so
`_unmet_pending_action` returns [] and `unmet()` misses the pending action.

The soft-cap check in agents.py now does a DIRECT `pending_action_phrase()`
check on the last iter's prose, bypassing the sequence-stamp logic, when:
  1. `_last_iter_prose` contains a pending-action phrase
  2. `_contract._usable_results > 0`  (data already retrieved)
  3. `_contract.forces_used < _contract.max_forces`

This test file validates the gap and the workaround at the contract level.
"""

from __future__ import annotations

import pytest

from app.services.goal_contract import (
    RESULT_QUALITY_ASSUMED_OK,
    GoalContract,
    build_goal_contract,
    pending_action_phrase,
)


# ── Gap: sequence-stamp logic misses promise-then-tool-calls ───────────────


def test_promise_then_tool_execution_satisfies_seq_stamp_but_phrase_remains():
    """When the model promises then executes, _executed_seq advances past
    _armed_seq, so unmet() returns []. But the promise text is still a
    pending-action phrase — the soft-cap check must detect this directly."""
    c = build_goal_contract("Give me supply chain data for last 30 days")
    # Query #1 succeeds
    c.record_tool_executed("execute_query", result_quality=RESULT_QUALITY_ASSUMED_OK)
    c.record_query_result([{"product_name": "C5", "revenue": 1000}])

    # Model produces promise text (would happen in LLM response alongside tool calls)
    _last_iter_prose = "Let me verify the revenue field is actually correct."
    c.refresh_pending_action(_last_iter_prose)
    assert c.pending_action_phrase is not None, "phrase must be armed"
    _armed_after_refresh = c._armed_seq

    # Tool execution AFTER arming advances _executed_seq past _armed_seq
    c.record_tool_executed("execute_query", result_quality=RESULT_QUALITY_ASSUMED_OK)
    assert c._executed_seq >= _armed_after_refresh, (
        "tool execution after arming advances _executed_seq"
    )

    # Sequence-stamp logic: unmet() returns [] (gap!)
    unmet_via_seq = c.unmet(granted_tools={"execute_query"})
    assert not any(cr.code == "pending_action" for cr in unmet_via_seq), (
        "sequence-stamp logic considers the promise satisfied — this is the gap"
    )

    # Direct phrase check: the promise is STILL in the last iter's prose
    phrase_still_present = pending_action_phrase(_last_iter_prose)
    assert phrase_still_present is not None, (
        "pending_action_phrase still detects the promise in the prose"
    )
    assert c._usable_results > 0, "usable data exists for forced synthesis"


def test_promise_phrase_survives_despite_satisfied_seq_stamp():
    """More complex turn: multiple queries, promise in last iter alongside
    a tool call. The seq stamp says 'satisfied' but the prose still promises."""
    c = build_goal_contract("Give me supply chain data for last 30 days")
    # Multiple queries already executed
    for _ in range(3):
        c.record_tool_executed("execute_query", result_quality=RESULT_QUALITY_ASSUMED_OK)
    c.record_query_result([{"product_name": "C5", "qty": 500}])

    # Last iter: model promises AND calls a tool
    _prose = (
        "I noticed Piperylene revenue was ¥0 and DCPD's amount looked "
        "suspiciously low. Let me verify the revenue field is actually "
        "correct rather than a blank-dimension/data issue."
    )
    c.refresh_pending_action(_prose)
    # Tool execution after the promise
    c.record_tool_executed("execute_query", result_quality=RESULT_QUALITY_ASSUMED_OK)

    # Sequence-stamp: satisfied
    unmet_via_seq = c.unmet(granted_tools={"execute_query"})
    assert not any(cr.code == "pending_action" for cr in unmet_via_seq)

    # Direct check: promise STILL present
    assert pending_action_phrase(_prose) is not None
    assert c._usable_results > 0
    assert c.forces_used < c.max_forces


# ── Soft-cap check: promise-as-answer bypasses seq-stamp ───────────────────


def test_soft_cap_promise_forces_synthesis_when_data_exists():
    """Simulates what the agents.py soft-cap check does when it detects a
    promise-as-answer: record a force and set _contract_force_synthesis."""
    c = build_goal_contract("Give me supply chain data for last 30 days")
    c.record_tool_executed("execute_query", result_quality=RESULT_QUALITY_ASSUMED_OK)
    c.record_query_result([{"product_name": "C5", "revenue": 1000}])

    _last_iter_prose = "Let me verify the revenue field is actually correct."
    c.refresh_pending_action(_last_iter_prose)
    c.record_tool_executed("execute_query", result_quality=RESULT_QUALITY_ASSUMED_OK)

    # Simulate soft-cap check logic (from agents.py):
    _gc_unmet = c.unmet(granted_tools={"execute_query"})
    _last_pending = pending_action_phrase(_last_iter_prose)
    _should_force = (
        not any(cr.code == "pending_action" for cr in _gc_unmet)
        and _last_pending is not None
        and c._usable_results > 0
        and c.forces_used < c.max_forces
    )
    assert _should_force, "soft-cap must force synthesis despite seq-stamp gap"

    # Simulate the force action
    c.record_force()
    assert c.forces_used == 1


def test_soft_cap_no_force_when_no_usable_data():
    """If no usable data exists, promise-as-answer should NOT force (model
    genuinely needs to execute the query)."""
    c = build_goal_contract("Give me supply chain data for last 30 days")
    # No queries executed — no usable data
    _last_iter_prose = "Let me query the sales data."
    c.refresh_pending_action(_last_iter_prose)

    _last_pending = pending_action_phrase(_last_iter_prose)
    _should_force = (
        _last_pending is not None
        and c._usable_results > 0  # This is 0
        and c.forces_used < c.max_forces
    )
    assert not _should_force, "no force when no usable data"


def test_soft_cap_no_force_when_force_budget_exhausted():
    """When the force budget is exhausted, promise-as-answer should NOT
    force another iteration (prevents infinite loops)."""
    c = build_goal_contract("Give me supply chain data for last 30 days")
    c.record_tool_executed("execute_query", result_quality=RESULT_QUALITY_ASSUMED_OK)
    c.record_query_result([{"product_name": "C5", "revenue": 1000}])

    # Exhaust the force budget
    for _ in range(c.max_forces):
        c.record_force()

    _last_iter_prose = "Let me verify the revenue field."
    c.refresh_pending_action(_last_iter_prose)
    c.record_tool_executed("execute_query", result_quality=RESULT_QUALITY_ASSUMED_OK)

    _last_pending = pending_action_phrase(_last_iter_prose)
    _should_force = (
        _last_pending is not None
        and c._usable_results > 0
        and c.forces_used < c.max_forces  # This is False now
    )
    assert not _should_force, "no force when budget exhausted"


def test_soft_cap_no_force_when_clean_prose():
    """When the last iter's prose has no pending-action phrase, no force."""
    c = build_goal_contract("Give me supply chain data for last 30 days")
    c.record_tool_executed("execute_query", result_quality=RESULT_QUALITY_ASSUMED_OK)
    c.record_query_result([{"product_name": "C5", "revenue": 1000}])

    _last_iter_prose = (
        "The total sales revenue for the last 30 days is ¥12.5M across "
        "all C5/C9 products. Inventory positions are stable."
    )
    # No pending phrase
    assert pending_action_phrase(_last_iter_prose) is None
    _should_force = (
        pending_action_phrase(_last_iter_prose) is not None
        and c._usable_results > 0
        and c.forces_used < c.max_forces
    )
    assert not _should_force, "no force when clean prose"


# ── Exact user-trace sentence ──────────────────────────────────────────────


def test_exact_user_trace_sentence_is_detected():
    """The exact sentence from the user's trace must be detected as a
    pending-action phrase by the direct check."""
    trace_sentence = (
        "The one gap worth closing: the user asked for supply chain data, "
        "and I noticed Piperylene revenue was ¥0 and DCPD's amount looked "
        "suspiciously low (¥1.93M for 6,181 t ≈ ¥312/t). Let me verify "
        "the revenue field is actually correct rather than a blank-dimension/data issue."
    )
    result = pending_action_phrase(trace_sentence)
    assert result is not None, "exact trace sentence must be detected"
    assert "let me" in result.lower()


# ── Extended empty-answer net (Fix 5): promise-only prose ───────────────────


def test_empty_answer_needs_force_with_only_promise_prose():
    """When accumulated_prose contains ONLY pending-action phrases and no
    substantive answer, _empty_answer_needs_force should fire — the promise
    text is not a real answer."""
    from app.routers.agents import _empty_answer_needs_force
    # No current iter prose, no streaming, but accumulated promise text
    result = _empty_answer_needs_force(
        assistant_content="",
        content_streamed=False,
        accumulated_prose=["Let me verify the revenue field is actually correct."],
        forces_used=0,
        has_usable_data=True,
    )
    assert result, "should force synthesis when only promise prose exists"


def test_empty_answer_needs_force_with_substantive_prose():
    """When the model's final reply is empty but earlier iterations produced
    substantive prose, the net now fires — earlier prose is intermediate
    narration, not a substitute for a synthesized final answer."""
    from app.routers.agents import _empty_answer_needs_force
    result = _empty_answer_needs_force(
        assistant_content="",
        content_streamed=False,
        accumulated_prose=[
            "Total sales revenue is ¥265.5M across 26 products.",
            "Let me verify the revenue field.",  # promise appended after
        ],
        forces_used=0,
        has_usable_data=True,
    )
    assert result, "should force even when earlier prose exists — final reply is empty"


def test_empty_answer_needs_force_no_data():
    """With only promise prose and no usable data, should not force."""
    from app.routers.agents import _empty_answer_needs_force
    result = _empty_answer_needs_force(
        assistant_content="",
        content_streamed=False,
        accumulated_prose=["Let me query the sales data."],
        forces_used=0,
        has_usable_data=False,
    )
    assert not result, "should NOT force when no usable data"


# ── Always-strip trailing pending (Fix 5 extension) ────────────────────────


def test_strip_trailing_pending_removes_promise_even_without_force_exhaustion():
    """The exit branch now ALWAYS strips promise text, not just when
    forces_used >= max_forces. This handles queries 1-2 where the model
    produces a good answer then appends 'Let me verify...'."""
    from app.routers.agents import _strip_trailing_pending
    text = (
        "Total sales revenue is ¥265.5M across 26 products. "
        "Product 103350 leads at ¥44.9M. "
        "Let me verify the revenue field is actually correct."
    )
    result = _strip_trailing_pending(text, "Let me verify the revenue field is actually correct.")
    assert "Let me verify" not in result
    assert "¥265.5M" in result  # substantive content preserved
