"""Sub-loop verification gate tests for the Data Agent.

Two tiers:
1. ``_sub_loop_answer_gate`` unit tests — verdict/action mapping, flag gate,
   TOTAL fallback, attempts/budget forwarding.
2. Loop integration via ``_ask_data_agent`` — a metadata-only answer must
   trigger the re-query nudge (agent re-plans with a real data query and then
   answers); flag-off must remain byte-identical (no gate, no extra LLM call).
"""
import asyncio
import os
import sys
from unittest.mock import patch

import pytest

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.config import settings
from app.services import agent_tools
from app.services.tool_handlers import delegation_tools
from app.services.answer_verification import VerificationResult

from tests.test_ask_data_agent_llm_failure import _db_with_kb  # noqa: E402


@pytest.fixture(autouse=True)
def _verification_flags(monkeypatch):
    """Deterministic-only verification on for these tests (no real LLM)."""
    monkeypatch.setattr(settings, "SELF_EVAL_REPLAN_ENABLED", True)
    monkeypatch.setattr(settings, "SELF_EVAL_LLM_GATE_ENABLED", False)
    monkeypatch.setattr(settings, "SELF_EVAL_MAX_REPLANS", 3)
    monkeypatch.setattr(settings, "DATA_AGENT_FASTPATH_ENABLED", False)
    yield
    monkeypatch.setattr(settings, "SELF_EVAL_REPLAN_ENABLED", False)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tier 1: _sub_loop_answer_gate
# ---------------------------------------------------------------------------


def test_gate_flag_off_skips(monkeypatch):
    monkeypatch.setattr(settings, "SELF_EVAL_REPLAN_ENABLED", False)
    action, msg = _run(delegation_tools._sub_loop_answer_gate(
        "top materials", {"last_metadata_rows": [{"table_name": "t"}]},
        "I found the schema", attempts=0, budget_remaining=3,
    ))
    assert action == ""
    assert msg == ""


def test_gate_no_tool_evidence_skips():
    action, msg = _run(delegation_tools._sub_loop_answer_gate(
        "top materials", {}, "no tools ran", attempts=0, budget_remaining=3,
    ))
    assert action == ""
    assert msg == ""


def test_gate_real_rows_complete():
    state = {"last_rows": [{"material": "A", "price": 10.0}], "last_sql": "SELECT ..."}
    action, msg = _run(delegation_tools._sub_loop_answer_gate(
        "what is the price of material A",
        state,
        "Material A costs 10",
        attempts=0, budget_remaining=3,
    ))
    assert action == ""
    assert msg == ""


def test_gate_metadata_only_nudges():
    state = {"last_metadata_rows": [{"table_name": "t", "table_rows": 100}]}
    action, msg = _run(delegation_tools._sub_loop_answer_gate(
        "what is the price of material A",
        state,
        "I found the table schema",
        attempts=0, budget_remaining=3,
    ))
    assert action == "nudge"
    assert "VERIFICATION GAP" in msg


def test_gate_blank_dimension_rows_nudge():
    """GAP 3: Data Agent rows whose name column is 100% blank must be
    intercepted by the sub-loop gate with a master-join nudge — not passed
    through un-flagged."""
    state = {
        "last_rows": [
            {"FCUSTMATNAME": "", "FMATERIALID": 1, "amount": 100},
            {"FCUSTMATNAME": None, "FMATERIALID": 2, "amount": 200},
        ],
        "last_sql": "SELECT FCUSTMATNAME, FMATERIALID, amount "
                    "FROM erp_product_sales_details",
    }
    action, msg = _run(delegation_tools._sub_loop_answer_gate(
        "show me last month sales data",
        state,
        "Here are the sales figures by product.",
        attempts=0, budget_remaining=3,
    ))
    assert action == "nudge"
    assert "VERIFICATION GAP" in msg
    assert "master" in msg.lower()


def test_gate_zero_rows_empty_nudges():
    """A real query that returned 0 rows must nudge (anti-pattern #3)."""
    state = {"last_rows": [], "last_sql": "SELECT ..."}
    action, msg = _run(delegation_tools._sub_loop_answer_gate(
        "top materials", state, "No rows were returned",
        attempts=0, budget_remaining=3,
    ))
    assert action == "nudge"


def test_gate_evaluator_raises_nonfatal(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("evaluator down")

    monkeypatch.setattr(delegation_tools, "evaluate_answer", boom)
    action, msg = _run(delegation_tools._sub_loop_answer_gate(
        "top materials",
        {"last_metadata_rows": [{"table_name": "t"}]},
        "I found the schema",
        attempts=0, budget_remaining=3,
    ))
    assert action == ""
    assert msg == ""


def test_gate_impossible_discloses(monkeypatch):
    monkeypatch.setattr(
        delegation_tools, "evaluate_answer",
        lambda *a, **k: VerificationResult(
            status="IMPOSSIBLE", gaps=["inventory unavailable"], suggested_fix="",
        ),
    )
    action, msg = _run(delegation_tools._sub_loop_answer_gate(
        "inventory levels",
        {"last_rows": [{"price": 1.0}]},
        "the price is 1",
        attempts=0, budget_remaining=3,
    ))
    assert action == "disclose"
    assert "Gap disclosure" in msg


def test_gate_forwards_attempts_and_budget(monkeypatch):
    captured = {}

    def _recorder(question, tool_results, final_text, **kwargs):
        captured["question"] = question
        captured["tool_results"] = tool_results
        captured["kwargs"] = kwargs
        return VerificationResult(status="COMPLETE")

    monkeypatch.setattr(delegation_tools, "evaluate_answer", _recorder)
    action, msg = _run(delegation_tools._sub_loop_answer_gate(
        "top materials",
        {"last_rows": [{"material": "A", "price": 1.0}]},
        "A costs 1",
        attempts=2, budget_remaining=1, endpoint="http://llm",
    ))
    assert action == ""
    assert captured["question"] == "top materials"
    assert captured["kwargs"]["attempts"] == 2
    assert captured["kwargs"]["budget_remaining"] == 1
    assert captured["kwargs"]["endpoint"] == "http://llm"
    assert captured["tool_results"][0]["tool"] == "execute_query"
    assert captured["tool_results"][0]["rows"] == [{"material": "A", "price": 1.0}]


def test_gate_all_null_aggregate_rows_nudge():
    """The C5/C9 'no data' bug: a query that returned a single all-NULL
    aggregate row (wrong column name + English filter literal) must be
    rejected by the gate — the agent is nudged to re-probe instead of
    answering 'no data'."""
    state = {
        "last_rows": [
            {"total_volume": None, "total_revenue": None, "product_count": None},
        ],
        "last_sql": "SELECT SUM(shipment_quantity) AS total_volume, ... "
                    "FROM erp_product_sales_details "
                    "WHERE material_name LIKE '%C5%'",
    }
    action, msg = _run(delegation_tools._sub_loop_answer_gate(
        "total sales for C5/C9 in July 2026",
        state,
        "No data available for July 2026.",
        attempts=0, budget_remaining=3,
    ))
    assert action == "nudge"
    assert "VERIFICATION GAP" in msg


def test_gate_all_null_payload_is_schema_shaped(monkeypatch):
    """All-NULL rows must be represented as columns + zero rows (NOT passed
    through as real rows), so the metadata-only detector fires."""
    captured = {}

    def _recorder(question, tool_results, final_text, **kwargs):
        captured["tool_results"] = tool_results
        return VerificationResult(status="COMPLETE")

    monkeypatch.setattr(delegation_tools, "evaluate_answer", _recorder)
    _run(delegation_tools._sub_loop_answer_gate(
        "total sales",
        {"last_rows": [{"total_volume": None, "total_revenue": None}]},
        "no data",
        attempts=0, budget_remaining=3,
    ))
    payload = captured["tool_results"][0]
    assert payload["columns"] == ["total_volume", "total_revenue"]
    assert payload["row_count"] == 0
    assert payload["rows"] == []


def test_gate_metadata_payload_is_schema_shaped(monkeypatch):
    """The metadata-only payload must carry columns + zero rows so the
    metadata-only detector fires inside evaluate_answer."""
    captured = {}

    def _recorder(question, tool_results, final_text, **kwargs):
        captured["tool_results"] = tool_results
        return VerificationResult(status="COMPLETE")

    monkeypatch.setattr(delegation_tools, "evaluate_answer", _recorder)
    _run(delegation_tools._sub_loop_answer_gate(
        "top materials",
        {"last_metadata_rows": [{"table_name": "t", "table_rows": 100}]},
        "schema",
        attempts=0, budget_remaining=3,
    ))
    payload = captured["tool_results"][0]
    assert payload["columns"] == ["table_name", "table_rows"]
    assert payload["row_count"] == 0
    assert payload["rows"] == []


# ---------------------------------------------------------------------------
# Tier 2: loop integration via _ask_data_agent
# ---------------------------------------------------------------------------


def _tool_call(sql):
    return {
        "content": "",
        "tool_calls": [{
            "id": "tc1", "type": "function",
            "function": {"name": "execute_query", "arguments": f'{{"sql":"{sql}"}}'},
        }],
    }


def _execute_tool_result(sql, rows):
    return {"success": True, "rows": rows, "sql": sql,
            "source": {"id": "kb-1", "name": "db_zhanlu_no1"}}


def test_loop_metadata_answer_triggers_replan(monkeypatch):
    """Metadata-only prose → gate nudge → agent re-plans with a real query →
    real rows → COMPLETE breaks with the real answer."""
    llm_responses = [
        _tool_call("SHOW TABLES"),
        {"content": "I found the schema.", "tool_calls": []},
        _tool_call("SELECT material, price FROM t"),
        {"content": "Material A costs 10.", "tool_calls": []},
    ]
    calls = []  # (messages, tools)

    async def fake_call_llm(messages, tools=None, endpoint=None):
        calls.append(messages)
        return llm_responses.pop(0)

    async def fake_execute_tool(tool_name, args, db, user_id, context=None):
        sql = (args or {}).get("sql", "")
        if "SHOW" in sql.upper():
            return _execute_tool_result(sql, [{"table_name": "t", "table_rows": 100}])
        return _execute_tool_result(sql, [{"material": "A", "price": 10.0}])

    db = _db_with_kb()
    with patch.object(delegation_tools, "_call_llm", side_effect=fake_call_llm), \
         patch.object(delegation_tools, "_call_llm_with_retry", side_effect=fake_call_llm), \
         patch.object(agent_tools, "execute_tool", side_effect=fake_execute_tool):
        result = _run(delegation_tools._ask_data_agent(
            args={
                "question": "what is the price of material A",
                "data_source_id": "kb-1",
                # The replan flow needs 4 turns (discover schema, prose, re-plan
                # query, real prose). The production default is 2 because the
                # [schema: ...] hint (delegation Fix 4) lets the sub-agent skip
                # describe_schema; this test exercises the fallback path where
                # the hint is absent, so it requests a larger budget explicitly.
                "max_iterations": 4,
            },
            db=db, user_id="u-1", context={"bound_kb_ids": ["kb-1"]},
        ))

    # The real answer must win (agent re-planned instead of stopping at schema).
    assert result["success"] is True, f"got {result}"
    assert "Material A costs 10" in result["answer"], f"got {result['answer']!r}"
    # 4 LLM calls: tool-call turn, metadata prose turn, re-plan tool turn, real prose turn.
    assert len(calls) == 4, f"expected 4 LLM calls, got {len(calls)}"
    # The nudge was injected into the conversation before the re-plan turn.
    nudge_turn = calls[2]
    assert any(
        isinstance(m.get("content"), str) and "VERIFICATION GAP" in m.get("content", "")
        for m in nudge_turn
    ), f"re-plan nudge missing from turn 3 messages"


def test_loop_real_rows_break_cleanly_no_extra_calls(monkeypatch):
    """A clean query → prose answer needs exactly 2 LLM calls (gate accepts,
    no extra nudges, no extra LLM round-trips)."""
    llm_responses = [
        _tool_call("SELECT material, price FROM t"),
        {"content": "Material A costs 10.", "tool_calls": []},
    ]
    calls = []

    async def fake_call_llm(messages, tools=None, endpoint=None):
        calls.append(messages)
        return llm_responses.pop(0)

    async def fake_execute_tool(tool_name, args, db, user_id, context=None):
        sql = (args or {}).get("sql", "")
        return _execute_tool_result(sql, [{"material": "A", "price": 10.0}])

    db = _db_with_kb()
    with patch.object(delegation_tools, "_call_llm", side_effect=fake_call_llm), \
         patch.object(delegation_tools, "_call_llm_with_retry", side_effect=fake_call_llm), \
         patch.object(agent_tools, "execute_tool", side_effect=fake_execute_tool):
        result = _run(delegation_tools._ask_data_agent(
            args={"question": "what is the price of material A", "data_source_id": "kb-1"},
            db=db, user_id="u-1", context={"bound_kb_ids": ["kb-1"]},
        ))

    assert result["success"] is True
    assert "Material A costs 10" in result["answer"]
    assert len(calls) == 2, f"expected 2 LLM calls (no gate overhead), got {len(calls)}"


def test_loop_flag_off_is_byte_identical(monkeypatch):
    """Flag off: gate never runs, metadata prose is accepted as-is."""
    monkeypatch.setattr(settings, "SELF_EVAL_REPLAN_ENABLED", False)
    llm_responses = [
        _tool_call("SHOW TABLES"),
        {"content": "I found the schema.", "tool_calls": []},
    ]
    calls = []

    async def fake_call_llm(messages, tools=None, endpoint=None):
        calls.append(messages)
        return llm_responses.pop(0)

    async def fake_execute_tool(tool_name, args, db, user_id, context=None):
        sql = (args or {}).get("sql", "")
        return _execute_tool_result(sql, [{"table_name": "t", "table_rows": 100}])

    db = _db_with_kb()
    with patch.object(delegation_tools, "_call_llm", side_effect=fake_call_llm), \
         patch.object(delegation_tools, "_call_llm_with_retry", side_effect=fake_call_llm), \
         patch.object(agent_tools, "execute_tool", side_effect=fake_execute_tool):
        result = _run(delegation_tools._ask_data_agent(
            args={"question": "what is the price of material A", "data_source_id": "kb-1"},
            db=db, user_id="u-1", context={"bound_kb_ids": ["kb-1"]},
        ))

    assert result["success"] is True
    assert "I found the schema" in result["answer"]
    assert len(calls) == 2, f"flag-off must not add gate calls, got {len(calls)}"
