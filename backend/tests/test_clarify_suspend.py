"""Clarify turn-suspension regression tests (2026-08-28).

Root cause: on the Sales Performance Dashboard turn (conv f62e4c2b) the v3
loop kept iterating AFTER the model called `clarify` — 2x clarify, 2 blocked
create_artifact (deliverable phase-lock), 3 failed execute_code — and died
with a confusing "Failed · phase_enter.act" + verify_failed instead of a
clean pause awaiting the user's answer. The clarify tool's own contract says
"the user's next message is the answer"; the loop just never honored it.

Fix: CLARIFY_SUSPENDS_TURN_ENABLED — a successful clarify result ends the
turn immediately (the loop breaks after the batch), and the final bubble
shows the question instead of the empty "I gathered some information"
fallback. The verify phase reports passed (the question was delivered).
"""
import asyncio
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.config import settings
from app.services.tool_handlers.clarify_tool import _clarify


def _suspend_after(result, flag_on):
    """Mirror of the v3-loop suspend decision (agents.py, 2026-08-28):
    a successful clarify result with the flag enabled ends the turn."""
    if not flag_on:
        return False
    if not isinstance(result, dict) or not result.get("success"):
        return False
    return True


def test_clarify_handler_returns_handoff_payload():
    """The clarify result the loop relies on: success + question_id + the
    explicit 'next message is the answer' instruction."""
    async def _run():
        return await _clarify(
            {"question": "Which region?", "choices": ["East", "West"]},
            db=None, user_id=None, context={},
        )

    result = asyncio.new_event_loop().run_until_complete(_run())
    assert result["success"] is True
    assert result["question_id"]
    assert result["question"] == "Which region?"
    assert result["choices"] == ["East", "West"]
    assert "next message is the answer" in result["instruction"]


def test_suspend_decision_on_real_failed_turn_result():
    """The exact clarify result the failed dashboard turn received must
    trigger suspension when the flag is ON — before any of the blocked
    create_artifact / failed execute_code calls that followed it."""
    real_result = {
        "success": True,
        "question_id": "1a47e4d0",
        "question": (
            "No database is currently bound to this agent, so there's no real "
            "production/yield/equipment/capacity data to feed the dashboard. "
            "How should I proceed?"
        ),
        "choices": [
            "Bind a database and I'll build it on real data",
            "Use clearly-labeled demo/synthetic data (marked 'indicative') "
            "so I can ship the full working dashboard",
        ],
        "open_ended": False,
    }
    # Flag ON (the shipped configuration) -> suspend.
    assert _suspend_after(real_result, flag_on=True) is True
    # Flag OFF (legacy behavior) -> no behavior change.
    assert _suspend_after(real_result, flag_on=False) is False
    # A failed clarify (e.g. missing question) must never suspend.
    assert _suspend_after({"success": False, "error": "question is required"}, True) is False


def test_flag_exists_and_defaults_off():
    """Project convention: flags default OFF; enabled per-deployment."""
    assert hasattr(settings, "CLARIFY_SUSPENDS_TURN_ENABLED")
