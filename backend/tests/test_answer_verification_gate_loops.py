"""Loop-boundary integration tests for the universal answer-verification gate.

Exercises ``_check_answer_verification_gate`` (the async helper wired at the
v2 / resume / v3 synthesis boundaries in ``agents.py``): COMPLETE accepts,
INCOMPLETE nudges+continues, IMPOSSIBLE discloses, and any evaluator failure
is non-fatal (never blocks the stream). The evaluator itself is mocked so
these tests are fast and deterministic.
"""
import asyncio

import pytest

from app.config import settings
from app.routers import agents


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
    """Flag on for the gate tests; each test can override via monkeypatch."""
    monkeypatch.setattr(settings, "SELF_EVAL_REPLAN_ENABLED", True)
    monkeypatch.setattr(settings, "SELF_EVAL_LLM_GATE_ENABLED", False)
    monkeypatch.setattr(settings, "SELF_EVAL_MAX_REPLANS", 3)
    yield
    monkeypatch.setattr(settings, "SELF_EVAL_REPLAN_ENABLED", False)


# ── flag-off: byte-identical behavior ───────────────────────────────────


def test_gate_flag_off_returns_none_without_calling_evaluator(monkeypatch):
    monkeypatch.setattr(settings, "SELF_EVAL_REPLAN_ENABLED", False)

    def _should_not_run(*a, **k):
        raise AssertionError("evaluate_answer must not run when flag is off")

    monkeypatch.setattr(agents, "evaluate_answer", _should_not_run)
    result = _run(agents._check_answer_verification_gate(
        "show me prices",
        _tool_calls(("execute_query", {"rows": [{"price": 1}]})),
        "the price is 1",
        attempts=0,
        budget_remaining=38,
    ))
    assert result.action == "none"
    assert result.message == ""


def test_gate_flag_off_with_tools_never_evaluates(monkeypatch):
    """Even with tools present, flag-off never evaluates."""
    monkeypatch.setattr(settings, "SELF_EVAL_REPLAN_ENABLED", False)
    calls = []

    def _recorder(*a, **k):
        calls.append(a)
        return None

    monkeypatch.setattr(agents, "evaluate_answer", _recorder)
    result = _run(agents._check_answer_verification_gate(
        "show me prices",
        _tool_calls(("execute_query", {"rows": [{"price": 1}]})),
        "the price is 1",
        attempts=0,
        budget_remaining=38,
    ))
    assert result.action == "none"
    assert calls == []


# ── no tool calls → skip ────────────────────────────────────────────────


def test_gate_empty_tool_calls_returns_none(monkeypatch):
    calls = []

    def _recorder(*a, **k):
        calls.append(a)
        return None

    monkeypatch.setattr(agents, "evaluate_answer", _recorder)
    result = _run(agents._check_answer_verification_gate(
        "show me prices",
        [],
        "I had trouble putting it together",
        attempts=0,
        budget_remaining=38,
    ))
    assert result.action == "none"
    assert calls == []


def test_gate_tool_calls_without_payloads_returns_none(monkeypatch):
    """A name-only record (no result payload) never triggers an action."""
    calls = []

    def _recorder(*a, **k):
        calls.append(a)
        return _result("COMPLETE")

    monkeypatch.setattr(agents, "evaluate_answer", _recorder)
    result = _run(agents._check_answer_verification_gate(
        "show me prices",
        _tool_calls(("web_search", None)),
        "here is what I found",
        attempts=0,
        budget_remaining=38,
    ))
    assert result.action == "none"
    assert calls  # evaluator ran (harmless) but verdict COMPLETE → no action


# ── verdict → action mapping ────────────────────────────────────────────


def test_gate_complete_returns_none(monkeypatch):
    monkeypatch.setattr(agents, "evaluate_answer", lambda *a, **k: _result("COMPLETE"))
    result = _run(agents._check_answer_verification_gate(
        "show me prices",
        _tool_calls(("execute_query", {"rows": [{"price": 1}]})),
        "the price is 1",
        attempts=0,
        budget_remaining=38,
    ))
    assert result.action == "none"
    assert result.message == ""


def test_gate_incomplete_returns_nudge(monkeypatch):
    monkeypatch.setattr(
        agents,
        "evaluate_answer",
        lambda *a, **k: _result(
            "INCOMPLETE",
            gaps=["volume dimension missing"],
            fix="retry with shipment_quantity from the sales table",
        ),
    )
    result = _run(agents._check_answer_verification_gate(
        "show me price and volume",
        _tool_calls(("execute_query", {"rows": [{"price": 1}]})),
        "the price is 1",
        attempts=1,
        budget_remaining=20,
    ))
    assert result.action == "nudge"
    assert "volume" in result.message
    assert "Re-plan" in result.message


def test_gate_impossible_returns_disclose(monkeypatch):
    monkeypatch.setattr(
        agents,
        "evaluate_answer",
        lambda *a, **k: _result(
            "IMPOSSIBLE",
            gaps=["inventory data not available"],
            fix="",
        ),
    )
    result = _run(agents._check_answer_verification_gate(
        "current inventory levels",
        _tool_calls(("execute_query", {"rows": [{"price": 1}]})),
        "the price is 1",
        # attempts=1: the 2026-08-26 fast-path short-circuits attempts=0
        # turns that already have rows (evaluator never called), which
        # would defeat this test's purpose.
        attempts=1,
        budget_remaining=38,
    ))
    assert result.action == "disclose"
    assert "inventory" in result.message


def test_gate_disclose_used_when_budget_exhausted(monkeypatch):
    """Real evaluator (deterministic-only): metadata-only result triggers the
    metadata detector; attempts at cap + budget gone → IMPOSSIBLE → disclose
    (escalation happens inside evaluate_answer, gate maps IMPOSSIBLE→disclose)."""
    result = _run(agents._check_answer_verification_gate(
        "show me prices",
        _tool_calls(("execute_query", {"columns": ["price"], "row_count": 0, "rows": []})),
        "I found the schema and 0 rows",
        attempts=3,
        budget_remaining=0,
    ))
    assert result.action == "disclose"
    assert result.message != ""


def test_gate_evaluator_raises_returns_none_nonfatal(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(agents, "evaluate_answer", boom)
    result = _run(agents._check_answer_verification_gate(
        "show me prices",
        _tool_calls(("execute_query", {"rows": [{"price": 1}]})),
        "the price is 1",
        attempts=0,
        budget_remaining=38,
    ))
    assert result.action == "none"
    assert result.message == ""


# ── attempts / budget forwarding ────────────────────────────────────────


def test_gate_forwards_attempts_and_budget_to_evaluator(monkeypatch):
    captured = {}

    def _recorder(*a, **k):
        captured.update(a=a, k=k)
        return _result("COMPLETE")

    monkeypatch.setattr(agents, "evaluate_answer", _recorder)
    result = _run(agents._check_answer_verification_gate(
        "show me prices",
        _tool_calls(("execute_query", {"rows": [{"price": 1}]})),
        "the price is 1",
        attempts=2,
        budget_remaining=7,
    ))
    assert result.action == "none"
    assert captured["a"][0] == "show me prices"
    assert captured["k"]["attempts"] == 2
    assert captured["k"]["budget_remaining"] == 7


def test_gate_extracts_results_from_various_record_shapes(monkeypatch):
    captured = {}

    def _recorder(*a, **k):
        captured["results"] = a[1]
        return _result("COMPLETE")

    monkeypatch.setattr(agents, "evaluate_answer", _recorder)
    result = _run(agents._check_answer_verification_gate(
        "show me prices",
        [
            {"name": "execute_query", "results": {"rows": [{"price": 1}], "columns": ["price"]}},
            {"name": "ask_data_agent", "results": {"rows": [{"material": "A"}]}},
            {"name": "web_search", "results": "text summary"},
            {"name": "web_search"},
        ],
        "the price is 1",
        # attempts=1: the 2026-08-26 fast-path short-circuits attempts=0
        # turns that already have rows (evaluator never called), which
        # would defeat this test's purpose.
        attempts=1,
        budget_remaining=38,
    ))
    names = [r.get("tool") for r in captured["results"]]
    # dict-shaped results keep payload; string payloads become text;
    # name-only records pass through as a bare tool marker.
    assert names == ["execute_query", "ask_data_agent", "web_search", "web_search"]
    text_rec = captured["results"][2]
    assert text_rec.get("text") == "text summary"


# ── helpers ─────────────────────────────────────────────────────────────


def _result(status, gaps=None, fix=""):
    from app.services.answer_verification import VerificationResult

    return VerificationResult(status=status, gaps=gaps or [], suggested_fix=fix)
