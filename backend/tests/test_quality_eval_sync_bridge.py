"""Regression tests for the async→sync bridge fixes (2026-08-17).

Three bugs caused the "Sorry, the connection was interrupted" /
"Operation interrupted." failures in agent chat streams:

1. FSM ``_llm_call`` (fsm.py) called the async ``call_llm`` WITHOUT await,
   returning a coroutine object instead of a result dict — every FSM
   quality eval silently fell back to the heuristic.
2. ``_sync_llm_bridge`` (quality_eval.py) used ``get_running_loop()`` +
   ``run_until_complete()``, which raises
   "RuntimeError: This event loop is already running" when invoked from
   inside an already-running loop (the v3 streaming path).
3. The non-FSM v3 path called ``evaluate_response_quality()`` synchronously
   inside the async SSE generator, blocking the event loop and killing
   heartbeats → proxy/browser timeout.

These tests lock the bridge behavior in both contexts (plain sync call,
and inside a running event loop via ``asyncio.to_thread``, mirroring the
production ``asyncio.to_thread`` wiring in agents.py).
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.synexia.fsm import ExecutionRequest, SynexiaFSM
from app.services.synexia.quality_eval import evaluate_response_quality


# ── helpers ───────────────────────────────────────────────────────────────


def _accept_json() -> str:
    return json.dumps({
        "verdict": "accept",
        "completeness_score": 0.9,
        "confidence": 0.9,
    })


async def _async_call_llm_stub(**kwargs):
    """Mirrors the real production call_llm: async, returns {"response": str}."""
    return {"response": _accept_json()}


def _make_fsm() -> SynexiaFSM:
    f = SynexiaFSM.__new__(SynexiaFSM)
    f.db = MagicMock()
    f.execution = SimpleNamespace(
        id="exec-bridge-test",
        observations=None,
        task_spec={"acceptance_criteria": ["include revenue figures"]},
        context_manifest=None,
        confidence_factors={},
        confidence_score=0.75,
        current_state="finalize",
    )
    f.plan = None
    f._last_verify_result = None
    return f


# ── evaluate_response_quality (via its internal _sync_llm_bridge) ─────────


def test_evaluate_response_quality_plain_sync_context(monkeypatch):
    """Async call_llm stub, no running loop → bridge runs it to completion."""
    monkeypatch.setattr(
        "app.services.llm_service.call_llm", _async_call_llm_stub
    )

    result = evaluate_response_quality(
        user_message="make a report",
        assistant_text="Here is the report with revenue figures.",
        max_iterations=0,
    )
    assert result is not None
    assert result.verdict == "accept"
    assert result.raw != "heuristic-only"  # real LLM path, not heuristic


def test_evaluate_response_quality_sync_stub_passthrough(monkeypatch):
    """Tests may inject a plain sync stub (dict, not coroutine) — must pass
    through untouched (no new_event_loop / run_until_complete on a dict)."""
    def _sync_stub(**kwargs):
        return {"response": _accept_json()}

    monkeypatch.setattr("app.services.llm_service.call_llm", _sync_stub)

    result = evaluate_response_quality(
        user_message="make a report",
        assistant_text="Here is the report with revenue figures.",
        max_iterations=0,
    )
    assert result is not None
    assert result.verdict == "accept"


def test_evaluate_response_quality_async_stub_via_to_thread(monkeypatch):
    """Full evaluate_response_quality through the bridge from a worker
    thread with the running loop active — the exact non-FSM v3 shape."""
    monkeypatch.setattr(
        "app.services.llm_service.call_llm", _async_call_llm_stub
    )

    async def main():
        def _sync_qe():
            return evaluate_response_quality(
                user_message="make a report",
                assistant_text="Here is the report with revenue figures.",
                max_iterations=0,  # eval-only
            )
        return await asyncio.to_thread(_sync_qe)

    result = asyncio.run(main())
    assert result is not None
    assert result.verdict == "accept"
    assert result.is_ok is True
    assert result.raw != "heuristic-only"  # real LLM path used, not fallback


# ── FSM _llm_call behavior ────────────────────────────────────────────────


def test_fsm_run_quality_eval_with_async_call_llm(monkeypatch):
    """REGRESSION (Bug A): previously call_llm was called without await, so
    `result.get("response")` crashed on a coroutine object and the FSM
    silently used the heuristic. With an async stub this must return a real
    accept verdict."""
    monkeypatch.setattr(
        "app.services.llm_service.call_llm", _async_call_llm_stub
    )
    fsm = _make_fsm()
    req = ExecutionRequest(
        conversation_id="conv-bridge-test",
        agent_name="general_assistant",
        user_message="make a sales report",
    )

    qer = fsm.run_quality_eval(req, "Here is the report with revenue figures.")
    assert qer is not None
    assert qer.verdict == "accept"
    assert qer.raw != "heuristic-only"
    assert fsm.execution.confidence_score == pytest.approx(0.75)  # accept → no penalty


def test_fsm_run_quality_eval_sync_stub_still_works(monkeypatch):
    """Existing tests patch call_llm with a plain sync lambda — the fixed
    bridge must still pass that through (regression guard for
    test_fsm_quality_eval.py)."""
    def _sync_stub(**kwargs):
        return {"response": _accept_json()}

    monkeypatch.setattr("app.services.llm_service.call_llm", _sync_stub)
    fsm = _make_fsm()
    req = ExecutionRequest(
        conversation_id="conv-bridge-sync",
        agent_name="general_assistant",
        user_message="make a sales report",
    )

    qer = fsm.run_quality_eval(req, "Here is the report with revenue figures.")
    assert qer is not None
    assert qer.verdict == "accept"
    assert "quality_eval" in fsm.execution.confidence_factors
