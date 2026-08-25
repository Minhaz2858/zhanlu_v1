"""Tests for qwen3.6-27b CEO-grade synthesis prompt.

Run in-container:
  /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_synthesis_quality_qwen3.py']))"
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from app.routers.agents import _build_qwen3_synthesis_prompt


def test_synthesis_prompt_has_markdown_scaffolding():
    """The qwen3 synthesis prompt must contain the 5-section markdown template."""
    rows = [{"product": "Widget", "revenue": 1000.0}]
    prompt = _build_qwen3_synthesis_prompt(
        user_question="show me sales",
        data_rows=rows,
        columns=["product", "revenue"],
        table_name="sales_data",
    )
    assert "## Executive Summary" in prompt
    assert "## Key Metrics" in prompt
    assert "## Detailed Breakdown" in prompt
    assert "## Risks & Opportunities" in prompt
    assert "## Recommended Actions" in prompt


def test_synthesis_prompt_inlines_data_rows():
    """The data rows must appear inline in the prompt (not as a tool result)."""
    rows = [{"product": "Widget", "revenue": 1000.0}]
    prompt = _build_qwen3_synthesis_prompt(
        user_question="show me sales",
        data_rows=rows,
        columns=["product", "revenue"],
        table_name="sales_data",
    )
    assert "Widget" in prompt
    assert "1,000" in prompt  # formatted with comma separator: 1,000.00
    assert "DATA (use only this" in prompt


def test_synthesis_prompt_truncates_large_data():
    """When >100 rows, only the first 100 are inlined (to fit context)."""
    rows = [{"product": f"Item{i}", "revenue": float(i)} for i in range(200)]
    prompt = _build_qwen3_synthesis_prompt(
        user_question="show me sales",
        data_rows=rows,
        columns=["product", "revenue"],
        table_name="sales_data",
    )
    assert "Item0" in prompt
    assert "Item99" in prompt
    assert "Item100" not in prompt  # truncated
    assert "200" in prompt  # total row count mentioned


def test_synthesis_max_tokens_is_3072_for_qwen3():
    """When endpoint is qwen3 local, synthesis max_tokens is 3072."""
    from app.routers.agents import _effective_synthesis_max_tokens
    from types import SimpleNamespace
    endpoint = SimpleNamespace(model_id="qwen3.6-27b", is_private=True, api_key="EMPTY")
    assert _effective_synthesis_max_tokens(endpoint) == 3072


def test_synthesis_max_tokens_is_legacy_for_deepseek():
    """When endpoint is deepseek, synthesis max_tokens stays at legacy."""
    from app.routers.agents import _effective_synthesis_max_tokens
    from types import SimpleNamespace
    endpoint = SimpleNamespace(model_id="deepseek-chat", is_private=False, api_key="sk-real")
    assert _effective_synthesis_max_tokens(endpoint) != 3072
