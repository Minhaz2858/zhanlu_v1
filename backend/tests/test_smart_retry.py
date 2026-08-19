"""Tests for the LLM-driven smart retry in synexia/smart_retry.py."""
import asyncio

import pytest

from app.services.synexia.smart_retry import _extract_question, llm_driven_retry_ask_data


def _empty_result(sql="SELECT 1 WHERE false", source_name="sales_db"):
    return {"rows": [], "sql": sql, "source_name": source_name, "question": "q"}


def _rows_result(sql="SELECT 1"):
    return {"rows": [{"a": 1}], "sql": sql, "source_name": "sales_db"}


def _run(coro):
    return asyncio.run(coro)


def test_retry_success_returns_rows():
    """LLM proposes a broader question → executor finds rows → returned."""
    calls = []

    async def call_llm_fn(sys_prompt, msgs):
        calls.append(msgs)
        return '{"question": "show all sales in the last 12 months"}'

    async def execute_ask_data_fn(q):
        assert "12 months" in q
        return _rows_result()

    out = _run(
        llm_driven_retry_ask_data(
            question="sales last month",
            failed_result=_empty_result(),
            call_llm_fn=call_llm_fn,
            execute_ask_data_fn=execute_ask_data_fn,
            max_attempts=2,
        )
    )
    assert out is not None
    assert out["rows"]
    assert out["retried"] is True
    assert out["retried_question"] == "show all sales in the last 12 months"
    assert out["retry_attempts"] == 1


def test_retry_exhausts_budget_returns_none():
    """Two attempts both empty → None, no infinite loop."""
    attempts = []

    async def call_llm_fn(sys_prompt, msgs):
        return "show all sales in the last 24 months"

    async def execute_ask_data_fn(q):
        attempts.append(q)
        return _empty_result(sql="SELECT 1 WHERE false")

    out = _run(
        llm_driven_retry_ask_data(
            question="sales last month",
            failed_result=_empty_result(),
            call_llm_fn=call_llm_fn,
            execute_ask_data_fn=execute_ask_data_fn,
            max_attempts=2,
        )
    )
    assert out is None
    assert len(attempts) == 2


def test_retry_llm_decline_returns_none():
    async def call_llm_fn(sys_prompt, msgs):
        return "NO_RETRY"

    async def execute_ask_data_fn(q):
        raise AssertionError("must not execute when LLM declines")

    out = _run(
        llm_driven_retry_ask_data(
            question="sales last month",
            failed_result=_empty_result(),
            call_llm_fn=call_llm_fn,
            execute_ask_data_fn=execute_ask_data_fn,
            max_attempts=2,
        )
    )
    assert out is None


def test_retry_second_attempt_recovers():
    """First retry still empty, second finds data → result from attempt 2."""
    counters = {"exec": 0}

    async def call_llm_fn(sys_prompt, msgs):
        return "show all sales ever"

    async def execute_ask_data_fn(q):
        counters["exec"] += 1
        if counters["exec"] == 1:
            return _empty_result(sql="SELECT 1 WHERE false")
        return _rows_result(sql="SELECT * FROM sales")

    out = _run(
        llm_driven_retry_ask_data(
            question="sales last month",
            failed_result=_empty_result(),
            call_llm_fn=call_llm_fn,
            execute_ask_data_fn=execute_ask_data_fn,
            max_attempts=2,
        )
    )
    assert out is not None
    assert out["retry_attempts"] == 2


def test_llm_error_is_swallowed():
    async def call_llm_fn(sys_prompt, msgs):
        raise RuntimeError("provider down")

    async def execute_ask_data_fn(q):
        raise AssertionError("must not execute")

    out = _run(
        llm_driven_retry_ask_data(
            question="q",
            failed_result=_empty_result(),
            call_llm_fn=call_llm_fn,
            execute_ask_data_fn=execute_ask_data_fn,
        )
    )
    assert out is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('show all sales', 'show all sales'),
        ('```json\n{"question": "show sales"}\n```', 'show sales'),
        ('{"question": "show sales"}', 'show sales'),
        ('[{"question": "a"}, {"question": "b"}]', 'a'),
        ('NO_RETRY', None),
        ('```\nNO_RETRY\n```', None),
        ('', None),
        (None, None),
    ],
)
def test_extract_question(raw, expected):
    assert _extract_question(raw) == expected
