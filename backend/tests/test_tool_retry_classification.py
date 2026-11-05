"""Phase 2.1 T3 — execute_tool_with_retry wired to the shared classifier.

The retry seam must: classify every failure (structured feedback for the
ReAct observation), never retry permission/terminal failures, never retry a
mutating tool without idempotency, only reformulate arg-fixable failures, and
honor a per-turn healing budget from context.

Run: pytest tests/test_tool_retry_classification.py -q
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from app.services import agent_tools as at
from app.services.execution_recovery import HealingBudget


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def no_tracer(monkeypatch):
    tracer = MagicMock()
    monkeypatch.setattr(at, "_get_tracer", lambda: tracer)
    return tracer


def test_permission_failure_never_retried_and_classified(monkeypatch, no_tracer):
    calls = {"n": 0}

    async def fake_execute(tool_name, arguments, db, user_id=None, context=None):
        calls["n"] += 1
        return {"success": False, "error": "Permission denied: table finances"}

    monkeypatch.setattr(at, "execute_tool", fake_execute)
    result = _run(at.execute_tool_with_retry(
        "query_database", {}, None, None,
        context={}, max_attempts=3, base_delay=0,
    ))
    assert calls["n"] == 1
    assert result["failure_category"] == "permission_denied"
    assert result["recovery_hint"]


def test_transient_failure_retried_within_budget(monkeypatch, no_tracer):
    calls = {"n": 0}

    async def fake_execute(tool_name, arguments, db, user_id=None, context=None):
        calls["n"] += 1
        if calls["n"] < 3:
            return {"success": False, "error": "connection reset by peer"}
        return {"success": True, "rows": [["ok"]]}

    monkeypatch.setattr(at, "execute_tool", fake_execute)
    result = _run(at.execute_tool_with_retry(
        "list_data_sources", {}, None, None,
        context={}, max_attempts=3, base_delay=0,
    ))
    assert calls["n"] == 3
    assert result["success"] is True


def test_transient_failure_stops_at_max_attempts(monkeypatch, no_tracer):
    calls = {"n": 0}

    async def fake_execute(tool_name, arguments, db, user_id=None, context=None):
        calls["n"] += 1
        return {"success": False, "error": "query timed out"}

    monkeypatch.setattr(at, "execute_tool", fake_execute)
    result = _run(at.execute_tool_with_retry(
        "list_data_sources", {}, None, None,
        context={}, max_attempts=2, base_delay=0,
    ))
    assert calls["n"] == 3  # 1 initial + 2 retries
    assert result["failure_category"] == "timeout"
    assert result["success"] is False


def test_mutating_tool_unknown_failure_not_retried(monkeypatch, no_tracer):
    calls = {"n": 0}

    async def fake_execute(tool_name, arguments, db, user_id=None, context=None):
        calls["n"] += 1
        return {"success": False, "error": "something odd happened"}

    monkeypatch.setattr(at, "execute_tool", fake_execute)
    result = _run(at.execute_tool_with_retry(
        "create_po_draft", {}, None, None,  # mutating registry name
        context={}, max_attempts=3, base_delay=0,
    ))
    assert calls["n"] == 1
    assert result["recovery_outcome"] and result["recovery_hint"]


def test_invalid_args_reformulated_once_then_success(monkeypatch, no_tracer):
    exec_calls = {"n": 0}

    async def fake_execute(tool_name, arguments, db, user_id=None, context=None):
        exec_calls["n"] += 1
        if arguments.get("fixed"):
            return {"success": True, "rows": [["ok"]]}
        return {"success": False, "error": "Invalid arguments: 'query' is a required property"}

    async def fake_reformulate(tool_name, arguments, error):
        return {**arguments, "fixed": True}

    monkeypatch.setattr(at, "execute_tool", fake_execute)
    monkeypatch.setattr(at, "_reformulate_tool_args", fake_reformulate)
    result = _run(at.execute_tool_with_retry(
        "execute_query", {}, None, None,
        context={}, max_attempts=1, base_delay=0,
    ))
    assert result["success"] is True
    assert result.get("reformulated") is True


def test_permission_failure_skips_reformulation(monkeypatch, no_tracer):
    async def fake_execute(tool_name, arguments, db, user_id=None, context=None):
        return {"success": False, "error": "Permission denied: writes blocked"}

    reformulate = MagicMock()

    async def fake_ref(**kw):
        return {}

    async def fake_reformulate(tool_name, arguments, error):
        return {"changed": True}

    monkeypatch.setattr(at, "execute_tool", fake_execute)
    monkeypatch.setattr(at, "_reformulate_tool_args", fake_reformulate)
    result = _run(at.execute_tool_with_retry(
        "execute_query", {}, None, None,
        context={}, max_attempts=1, base_delay=0,
    ))
    assert result.get("reformulated") is None
    assert result["failure_category"] == "permission_denied"


def test_empty_result_not_retried_not_failure(monkeypatch, no_tracer):
    calls = {"n": 0}

    async def fake_execute(tool_name, arguments, db, user_id=None, context=None):
        calls["n"] += 1
        return {"success": True, "rows": []}

    monkeypatch.setattr(at, "execute_tool", fake_execute)
    result = _run(at.execute_tool_with_retry(
        "execute_query", {}, None, None,
        context={}, max_attempts=3, base_delay=0,
    ))
    assert calls["n"] == 1
    assert result.get("failure_category") is None  # success — no failure metadata


def test_healing_budget_in_context_bounds_retries(monkeypatch, no_tracer):
    calls = {"n": 0}

    async def fake_execute(tool_name, arguments, db, user_id=None, context=None):
        calls["n"] += 1
        return {"success": False, "error": "connection reset"}

    monkeypatch.setattr(at, "execute_tool", fake_execute)
    budget = HealingBudget(max_healings=1)
    result = _run(at.execute_tool_with_retry(
        "list_data_sources", {}, None, None,
        context={"_healing_budget": budget}, max_attempts=5, base_delay=0,
    ))
    # Budget of 1 allows exactly ONE healing attempt (1 initial + 1 retry).
    assert calls["n"] == 2
    assert result["success"] is False


def test_budget_exhausted_blocks_even_first_retry(monkeypatch, no_tracer):
    calls = {"n": 0}

    async def fake_execute(tool_name, arguments, db, user_id=None, context=None):
        calls["n"] += 1
        return {"success": False, "error": "connection reset"}

    monkeypatch.setattr(at, "execute_tool", fake_execute)
    budget = HealingBudget(max_healings=0)
    _run(at.execute_tool_with_retry(
        "list_data_sources", {}, None, None,
        context={"_healing_budget": budget}, max_attempts=5, base_delay=0,
    ))
    assert calls["n"] == 1
