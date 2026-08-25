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


# ── Hermes catalog injection (2026-08-25) ────────────────────────────────


def test_hermes_catalog_injected_into_prompt():
    """When ``get_catalog_fn`` is supplied, the catalog text appears in the
    system prompt sent to ``call_llm_fn``. This is the core of the Hermes
    re-plan: re-read the warehouse catalog so the LLM can pick a DIFFERENT
    table than the one that returned 0 rows."""
    seen_prompts = []

    async def call_llm_fn(sys_prompt, msgs):
        seen_prompts.append(sys_prompt)
        return "show all sales from erp_t_sal_outstock"

    async def execute_ask_data_fn(q):
        return _rows_result(sql="SELECT * FROM erp_t_sal_outstock")

    async def get_catalog_fn():
        return (
            "erp_t_sal_outstock | 14275 rows | coverage 2018-2026\n"
            "erp_t_ar_receipt | 8200 rows | coverage 2019-2026"
        )

    out = _run(
        llm_driven_retry_ask_data(
            question="last month sales",
            failed_result=_empty_result(),
            call_llm_fn=call_llm_fn,
            execute_ask_data_fn=execute_ask_data_fn,
            max_attempts=2,
            get_catalog_fn=get_catalog_fn,
        )
    )
    assert out is not None
    assert out["rows"]
    assert out["hermes_catalog_used"] is True
    # The catalog must appear in the prompt sent to the LLM
    assert seen_prompts, "call_llm_fn was never invoked"
    assert "erp_t_sal_outstock" in seen_prompts[0]
    assert "WAREHOUSE TABLE CATALOG" in seen_prompts[0]


def test_hermes_catalog_fetch_failure_is_non_fatal():
    """If ``get_catalog_fn`` raises, smart retry continues with just the
    failure context (legacy behavior). The retry must not crash."""
    async def call_llm_fn(sys_prompt, msgs):
        # Verify the catalog block is absent
        assert "WAREHOUSE TABLE CATALOG" not in sys_prompt
        return "show all sales"

    async def execute_ask_data_fn(q):
        return _rows_result()

    async def get_catalog_fn():
        raise RuntimeError("DB connection refused")

    out = _run(
        llm_driven_retry_ask_data(
            question="q",
            failed_result=_empty_result(),
            call_llm_fn=call_llm_fn,
            execute_ask_data_fn=execute_ask_data_fn,
            max_attempts=1,
            get_catalog_fn=get_catalog_fn,
        )
    )
    assert out is not None
    assert out["rows"]
    assert out["hermes_catalog_used"] is False


def test_hermes_catalog_not_fetched_when_not_supplied():
    """Backward-compat: when ``get_catalog_fn`` is None (default), no
    catalog fetch is attempted. The prompt contains only the failure
    context (legacy behavior)."""
    seen_prompts = []

    async def call_llm_fn(sys_prompt, msgs):
        seen_prompts.append(sys_prompt)
        return "show all sales"

    async def execute_ask_data_fn(q):
        return _rows_result()

    out = _run(
        llm_driven_retry_ask_data(
            question="q",
            failed_result=_empty_result(),
            call_llm_fn=call_llm_fn,
            execute_ask_data_fn=execute_ask_data_fn,
            max_attempts=1,
            # get_catalog_fn intentionally omitted
        )
    )
    assert out is not None
    assert out["hermes_catalog_used"] is False
    assert "WAREHOUSE TABLE CATALOG" not in seen_prompts[0]


def test_hermes_catalog_re_read_each_attempt():
    """The catalog is re-read before EACH retry attempt, not cached. This
    lets the LLM see fresh row counts if the warehouse updates between
    retries."""
    fetch_count = {"calls": 0}

    async def call_llm_fn(sys_prompt, msgs):
        # Every prompt must contain a catalog block (catalog was fetched
        # before each LLM call)
        assert "WAREHOUSE TABLE CATALOG" in sys_prompt
        return "show all sales"

    async def execute_ask_data_fn(q):
        # First exec empty, second exec returns rows
        fetch_count["exec"] = fetch_count.get("exec", 0) + 1
        if fetch_count["exec"] == 1:
            return _empty_result()
        return _rows_result()

    async def get_catalog_fn():
        fetch_count["calls"] += 1
        return f"table_{fetch_count['calls']} | {fetch_count['calls'] * 1000} rows"

    out = _run(
        llm_driven_retry_ask_data(
            question="q",
            failed_result=_empty_result(),
            call_llm_fn=call_llm_fn,
            execute_ask_data_fn=execute_ask_data_fn,
            max_attempts=2,
            get_catalog_fn=get_catalog_fn,
        )
    )
    assert out is not None
    assert out["retry_attempts"] == 2
    # Catalog fetched twice: once before attempt 1, once before attempt 2
    assert fetch_count["calls"] == 2
