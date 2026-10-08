"""Deterministic pending-promise detector.

When the model's draft answer announces a future action ("Let me re-query…")
the turn must be treated as unfinished.  ``goal_contract.pending_action_phrase``
is the deterministic detector for that — no LLM call is involved.

(The answer-verification gate that used to consume this detector was removed in
favour of ``app.services.goal_contract``; only the detector contract is
asserted here.)
"""

from __future__ import annotations

from app.services.goal_contract import pending_action_phrase


def test_pending_action_phrase_detects_promise() -> None:
    """Sanity: pending_action_phrase returns a match for promise drafts."""
    assert pending_action_phrase(
        "The first table was stale. Let me re-query against the live tables."
    ) is not None


def test_pending_action_phrase_clean_prose_is_none() -> None:
    assert pending_action_phrase(
        "The total sales revenue for the last 30 days is $1.2M."
    ) is None
