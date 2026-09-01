"""Tests for the data-sufficient fast-path (2026-08-25).

The fast-path is an inline guard in the v3 streaming loop (agents.py)
that skips the self-eval verification gate when the agent has already
produced substantive prose (>= SELF_EVAL_DATA_SUFFICIENT_MIN_PROSE chars)
AND has usable data AND has already nudged once.

These tests verify:
1. The pending_action downgrade in answer_verification.py: when
   pending_action is the only signal AND data + substantive prose
   exist, return COMPLETE (so the gate would map to "none" instead
   of "nudge" — the v3 loop's fast-path can then skip subsequent
   re-iterations).

2. The gate correctly maps VerificationResult.COMPLETE → action="none"
   (no nudge, fast-path stays safe), INCOMPLETE → "nudge", and
   IMPOSSIBLE → "disclose".

3. Edge cases: short prose, no data, multi-signal (pending_action +
   coverage) all return INCOMPLETE so the gate still nudges (no
   premature COMPLETE that would hide a real quality issue).
"""
import asyncio

import pytest

from app.config import settings
from app.routers import agents
from app.services import answer_verification as av


def _run(coro):
    return asyncio.run(coro)


def _tool_calls(*records):
    """Build tool_calls_for_frontend-shaped records."""
    out = []
    for rec in records:
        name, results = rec
        out.append({"name": name, "results": results})
    return out


@pytest.fixture(autouse=True)
def _enable_verification_flag(monkeypatch):
    """Flag on for these tests; SELF_EVAL_MAX_REPLANS=1 to mirror .env."""
    monkeypatch.setattr(settings, "SELF_EVAL_REPLAN_ENABLED", True)
    monkeypatch.setattr(settings, "SELF_EVAL_LLM_GATE_ENABLED", False)
    monkeypatch.setattr(settings, "SELF_EVAL_MAX_REPLANS", 1)
    monkeypatch.setattr(settings, "SELF_EVAL_DATA_SUFFICIENT_MIN_PROSE", 200)
    yield
    monkeypatch.setattr(settings, "SELF_EVAL_REPLAN_ENABLED", False)


# ── Fast-path enabled: evaluator returns COMPLETE for data-sufficient ──


def test_data_sufficient_pending_action_only_returns_none_action(monkeypatch):
    """The v3 fast-path condition maps to a gate result of "none" (no nudge).

    When the agent has substantive prose (>= 200 chars) AND usable data
    AND the only signal is pending_action, evaluate_answer returns
    COMPLETE. The gate then maps COMPLETE → action="none" with empty
    message — this is exactly the condition the v3 fast-path
    short-circuits to (skipping the call entirely once attempts >= 1).
    """
    long_prose = (
        "Sales summary for last month: PVC was the top seller with 1,200 units, "
        "followed by C5 with 800 units and C9 with 450 units. Customer mix "
        "remained stable, with the top three partners accounting for 60% of "
        "shipment volume. Average contract price held steady at the prior "
        "month's level, indicating stable demand across the portfolio and no "
        "material shift in customer behavior. We can monitor the daily "
        "shipment cadence to confirm the trend holds for the current month."
    )
    assert len(long_prose) >= 200
    result = _run(agents._check_answer_verification_gate(
        "give me last month sales report",
        _tool_calls((
            "execute_query",
            {"rows": [
                {"FNAME": "PVC", "units": 1200},
                {"FNAME": "C5", "units": 800},
                {"FNAME": "C9", "units": 450},
            ]},
        )),
        long_prose + " Let me verify these numbers against the database.",
        attempts=0,
        budget_remaining=38,
    ))
    assert result.action == "none", (
        f"expected 'none' for data-sufficient pending_action-only signal "
        f"(fast-path target), got action={result.action!r} message={result.message!r}"
    )
    assert result.message == ""


# ── Fast-path disabled by short prose: gate still nudges ───────────────


def test_short_prose_pending_action_still_nudges(monkeypatch):
    """Short prose does not trigger the fast-path; gate returns 'nudge'."""
    result = _run(agents._check_answer_verification_gate(
        "give me last month sales report",
        _tool_calls((
            "execute_query",
            {"rows": [{"FNAME": "PVC", "units": 1200}]},
        )),
        "Top seller: PVC. Let me check details.",
        attempts=0,
        budget_remaining=38,
    ))
    assert result.action == "nudge"


# ── MAX_REPLANS=1: after 1 attempt, INCOMPLETE escalates to IMPOSSIBLE ──


def test_max_replans_one_escalates_to_disclose(monkeypatch):
    """With MAX_REPLANS=1 and attempts=1, INCOMPLETE → IMPOSSIBLE → disclose.

    This is the upper bound on the data-sufficient fast-path: even if
    the fast-path doesn't fire, the gate cannot nudge more than once.
    After 1 nudge, the answer is disclosed with a gap statement.
    """
    result = _run(agents._check_answer_verification_gate(
        "give me last month sales report",
        _tool_calls((
            "execute_query",
            {"columns": ["x"], "rows": []},  # empty result → empty signal
        )),
        "No data here.",  # short prose → no fast-path
        attempts=1,  # at the cap
        budget_remaining=38,
    ))
    assert result.action == "disclose"
    assert result.message != ""


# ── Direct evaluate_answer boundary tests for the pending_action downgrade


def test_evaluate_answer_pending_action_only_data_sufficient_is_complete(monkeypatch):
    """Direct test of the pending_action downgrade: COMPLETE returned."""
    long_prose = (
        "Sales summary for last month: PVC was the top seller with 1,200 units, "
        "followed by C5 with 800 units and C9 with 450 units. Customer mix "
        "remained stable, with the top three partners accounting for 60% of "
        "shipment volume. Average contract price held steady at the prior "
        "month's level, indicating stable demand across the portfolio and no "
        "material shift in customer behavior. We can monitor the daily "
        "shipment cadence to confirm the trend holds for the current month."
    )
    assert len(long_prose) >= 200
    result = av.evaluate_answer(
        "give me last month sales report",
        [
            {
                "tool": "execute_query",
                "columns": ["FNAME", "units"],
                "rows": [
                    {"FNAME": "PVC", "units": 1200},
                    {"FNAME": "C5", "units": 800},
                    {"FNAME": "C9", "units": 450},
                ],
            }
        ],
        long_prose + " Let me verify these numbers against the database.",
        attempts=0,
        budget_remaining=38,
    )
    assert result.status == "COMPLETE"
    assert "pending_action_downgraded" in result.signals


def test_evaluate_answer_pending_action_short_prose_not_downgraded(monkeypatch):
    """Short prose does not trigger the downgrade → INCOMPLETE."""
    result = av.evaluate_answer(
        "give me last month sales report",
        [
            {
                "tool": "execute_query",
                "columns": ["FNAME", "units"],
                "rows": [{"FNAME": "PVC", "units": 1200}],
            }
        ],
        "Top seller: PVC. Let me check details.",
        attempts=0,
        budget_remaining=38,
    )
    assert result.status == "INCOMPLETE"
    assert "pending_action" in result.signals
    assert "pending_action_downgraded" not in result.signals
