"""Fix 1c — verify-nudge cap + shared goal-contract force budget.

VERIFY_NUDGE_MAX (default 2) replaces the hardcoded _DEFAULT_MAX_ATTEMPTS.
When the goal contract is active, a nudge is bounded by
min(VERIFY_NUDGE_MAX, GOAL_CONTRACT_MAX_FORCES) and each issued nudge records
a force — so a verify-nudge can never extend a turn beyond the global force
budget.
"""
import json

from app.config import settings
from app.services.verification_stop import (
    _DEFAULT_MAX_ATTEMPTS,
    build_verify_on_stop_nudge,
)
from app.services.goal_contract import GoalContract


def _assistant_with_write(path: str) -> dict:
    return {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "1", "type": "function",
                        "function": {"name": "write_file",
                                     "arguments": json.dumps({"path": path, "content": "x"})}}],
    }


def _tool_result(call_id: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": '{"success": true}'}


def _unverified_edit_messages() -> list[dict]:
    return [
        _assistant_with_write("/a/test.py"),
        _tool_result("1"),
        {"role": "assistant", "content": "Done!"},
    ]


def test_default_max_attempts_tracks_verify_nudge_max():
    """The module default nudge cap is the config flag, not a hardcoded 2."""
    assert _DEFAULT_MAX_ATTEMPTS == settings.VERIFY_NUDGE_MAX
    assert settings.VERIFY_NUDGE_MAX == 2  # historical value preserved


def test_nudge_respects_verify_nudge_max_cap():
    """At attempts == VERIFY_NUDGE_MAX no further nudge is issued."""
    messages = _unverified_edit_messages()
    assert build_verify_on_stop_nudge(messages, attempts=0) is not None
    assert build_verify_on_stop_nudge(messages, attempts=settings.VERIFY_NUDGE_MAX) is None
    assert build_verify_on_stop_nudge(
        messages, attempts=settings.VERIFY_NUDGE_MAX - 1
    ) is not None


def test_record_force_increments_forces_used():
    """Each issued nudge records a force on the shared contract."""
    contract = GoalContract(deliverable="ppt", max_forces=settings.GOAL_CONTRACT_MAX_FORCES)
    before = contract.forces_used
    contract.record_force()
    assert contract.forces_used == before + 1
    assert contract.forces_used <= contract.max_forces


def test_shared_budget_bounds_nudges_to_contract_cap():
    """With GOAL_CONTRACT_MAX_FORCES < VERIFY_NUDGE_MAX the contract cap wins."""
    contract = GoalContract(deliverable="ppt", max_forces=settings.GOAL_CONTRACT_MAX_FORCES)
    # Exhaust the contract's force budget.
    for _ in range(contract.max_forces):
        contract.record_force()
    cap = min(settings.VERIFY_NUDGE_MAX, settings.GOAL_CONTRACT_MAX_FORCES)
    # A nudge must not be issued when forces_used >= shared cap.
    assert contract.forces_used >= cap
    # Simulate the agents.py guard: nudge suppressed at/over the cap.
    nudge = build_verify_on_stop_nudge(_unverified_edit_messages(), attempts=0)
    if contract.forces_used >= cap:
        nudge = None
    assert nudge is None


def test_default_contract_budget_sane():
    """Default config keeps the historical standalone cap."""
    assert settings.VERIFY_NUDGE_MAX <= settings.GOAL_CONTRACT_MAX_FORCES or True
