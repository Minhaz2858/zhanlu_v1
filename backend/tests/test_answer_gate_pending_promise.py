"""Deterministic pending-promise detector in the answer-verification gate.

When the model's draft answer announces a future action ("Let me re-query…")
the gate must return INCOMPLETE without calling the 15s LLM evaluator — a
pending-promise is trivially detectable via goal_contract.pending_action_phrase.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from app.services.answer_verification import evaluate_answer, _evaluate_answer_inner
from app.services.goal_contract import pending_action_phrase


# ── the detector: pending_action_phrase on a promise draft ────────────────


def test_pending_action_phrase_detects_promise() -> None:
    """Sanity: pending_action_phrase returns a match for promise drafts."""
    assert pending_action_phrase(
        "The first table was stale. Let me re-query against the live tables."
    ) is not None


def test_pending_action_phrase_clean_prose_is_none() -> None:
    assert pending_action_phrase(
        "The total sales revenue for the last 30 days is $1.2M."
    ) is None


# ── gate-level: promise draft → INCOMPLETE without LLM evaluator ────────


def test_promise_draft_returns_incomplete_without_llm_call() -> None:
    """A draft that announces a future action must be flagged as INCOMPLETE
    by the deterministic detector, and the LLM evaluator must NOT be called."""
    tool_results = [{"rows": [{"product": "C5", "revenue": 1000}], "sql": "SELECT * FROM t"}]
    draft = (
        "The first attempt used a stale table. "
        "Let me re-query against the live, current tables that cover through 2026."
    )
    with patch(
        "app.services.answer_verification._run_llm_eval",
        side_effect=AssertionError("LLM evaluator must NOT be called"),
    ) as mock_llm:
        result = evaluate_answer(
            user_message="Give me supply chain data for last 30 days",
            tool_results=tool_results,
            assistant_text=draft,
            attempts=0,
            budget_remaining=10,
        )
        mock_llm.assert_not_called()
    assert result.status == "INCOMPLETE"
    assert "pending_action" in result.signals


def test_clean_draft_calls_llm_evaluator() -> None:
    """A clean draft (no pending promise) with non-trivial results should
    reach the LLM evaluator (or at least not short-circuit on pending_action)."""
    tool_results = [{"rows": [{"product": "C5", "revenue": 1000}], "sql": "SELECT * FROM t"}]
    draft = "The total sales revenue for the last 30 days is $1.2M across C5/C9 products."
    # With SELF_EVAL_LLM_GATE_ENABLED=false, the gate returns COMPLETE
    # deterministically. With it true, the LLM would be called. We just
    # verify that pending_action is NOT in the signals.
    with patch("app.services.answer_verification._run_llm_eval") as mock_llm:
        mock_llm.return_value = ("COMPLETE", [], "")
        result = evaluate_answer(
            user_message="Give me supply chain data for last 30 days",
            tool_results=tool_results,
            assistant_text=draft,
            attempts=0,
            budget_remaining=10,
        )
    assert "pending_action" not in result.signals
