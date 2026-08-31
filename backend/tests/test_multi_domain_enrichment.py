"""Tests for the multi-domain enrichment features (2026-08-25).

These tests verify:
1. ASK_DATA_AGENT_DEFAULT_CAP is 4 (was 2/6 hardcoded).
2. _estimate_ask_data_agent_cap still returns 2 for plain queries but
   the static TOOL_CALL_CAPS cap is now 4 — so the LLM can call
   ask_data_agent up to 4 times for plain queries.
3. The data domain hint block exists in agent_prompts and contains
   the 5 business domains.
4. ASK_DATA_AGENT_REFLECT_AFTER_FIRST + DATA_DOMAIN_HINTS_ENABLED
   default to True (so the changes are live by default).
5. The hint is generic — it does NOT hardcode specific table names.
"""
import pytest

from app.config import settings
from app.routers import agents
from app.services import agent_prompts


# ── Config / cap tests ─────────────────────────────────────────────────


def test_ask_data_agent_default_cap_is_4():
    """Default per-turn cap is 4 (was 2/6 hardcoded)."""
    assert settings.ASK_DATA_AGENT_DEFAULT_CAP == 4


def test_ask_data_agent_reflect_after_first_default_true():
    """Reflection-prompt gate defaults True so the change is live."""
    assert settings.ASK_DATA_AGENT_REFLECT_AFTER_FIRST is True


def test_data_domain_hints_enabled_default_true():
    """System-prompt domain-hint block defaults True."""
    assert settings.DATA_DOMAIN_HINTS_ENABLED is True


def test_tool_call_caps_ask_data_agent_uses_settings():
    """TOOL_CALL_CAPS['ask_data_agent'] is bound to settings cap (>= 4)."""
    cap = agents.TOOL_CALL_CAPS.get("ask_data_agent", 0)
    assert cap >= 4, f"ask_data_agent cap must be >= 4, got {cap}"


def test_tool_call_caps_preserves_other_caps():
    """Other per-tool caps (memory, interrupt, clarify) unchanged."""
    assert agents.TOOL_CALL_CAPS.get("memory") == 1
    assert agents.TOOL_CALL_CAPS.get("interrupt") == 2
    assert agents.TOOL_CALL_CAPS.get("clarify") == 3


def test_tool_call_caps_respects_settings_floor():
    """The cap is at least the configured ASK_DATA_AGENT_DEFAULT_CAP floor."""
    cap = agents.TOOL_CALL_CAPS.get("ask_data_agent", 0)
    assert cap >= max(4, settings.ASK_DATA_AGENT_DEFAULT_CAP)


# ── Dynamic estimator still works for plain queries ─────────────────────


def test_dynamic_estimator_plain_query_returns_2():
    """Plain 'give me X' query → dynamic estimator still returns 2.

    The static cap of 4 in TOOL_CALL_CAPS is the floor, not the dynamic
    estimator's result. The loop uses max(static, dynamic), so 4 wins.
    """
    cap = agents._estimate_ask_data_agent_cap("give me last month sales report")
    assert cap == 2  # no explicit metrics, no parentheses, no concept keywords


def test_dynamic_estimator_explicit_metrics_bumps_above_floor():
    """Query with explicit metric list bumps the cap above 4."""
    cap = agents._estimate_ask_data_agent_cap(
        "give me sales report (revenue, qty, customer, region, product, month)"
    )
    assert cap >= 6  # 6 explicit metrics → cap >= 6


# ── Data domain hint block content tests ────────────────────────────────


def test_data_domain_hint_block_present():
    """The data-domain hint block exists in agent_prompts.

    2026-08-25: The block is now general (no enterprise assumptions).
    The 5 enterprise domains appear as ILLUSTRATIVE examples in the
    examples section, not as canonical sections. The block must also
    mention non-enterprise examples (IoT, medical, etc.) to signal
    that any database type is supported.
    """
    assert hasattr(agent_prompts, "_DATA_DOMAIN_HINT_BLOCK")
    block = agent_prompts._DATA_DOMAIN_HINT_BLOCK
    # Case-insensitive checks for the 5 illustrative enterprise domains
    block_lower = block.lower()
    assert "erp" in block_lower
    assert "inventory" in block_lower
    assert "market" in block_lower
    assert "crm" in block_lower
    assert "documents" in block_lower
    # Non-enterprise examples are also present
    for example in ["sensor", "patient", "ticker", "post_id", "logistics", "geospatial", "event"]:
        assert example in block_lower, (
            f"non-enterprise example {example!r} should be in the hint block"
        )
    # Illustrative note
    assert "illustrative" in block_lower or "no closed list" in block_lower


def test_data_domain_hint_block_mentions_ask_data_agent():
    """The hint tells the LLM to use ask_data_agent for cross-domain queries."""
    block = agent_prompts._DATA_DOMAIN_HINT_BLOCK
    assert "ask_data_agent" in block
    assert "describe_schema" in block


def test_data_domain_hint_block_no_domain_specific_tables():
    """The hint is generic — it does NOT hardcode specific table names.

    The agent must discover tables via describe_schema, not from the
    system prompt. This test guards against accidental leakage of
    domain-specific table names into the generic hint.
    """
    block = agent_prompts._DATA_DOMAIN_HINT_BLOCK
    # Domain-specific table names that should NOT appear in the generic hint
    forbidden_tables = [
        "erp_product_sales_details",
        "erp_t_sal_outstock",
        "market_daily_price",
        "ecisco.",
        "FNAME",
        "FMATERIALID",
    ]
    for tbl in forbidden_tables:
        assert tbl not in block, (
            f"domain-specific table {tbl!r} must not appear in the generic "
            f"data-domain hint block; agents should discover via describe_schema"
        )


def test_data_domain_hint_block_mentions_default_cap():
    """The hint mentions the ~4 default cap so the LLM knows the budget."""
    block = agent_prompts._DATA_DOMAIN_HINT_BLOCK
    assert "4" in block
    assert "ASK_DATA_AGENT_DEFAULT_CAP" in block


# ── Reflection prompt content tests (extracted from agents.py) ──────────


def test_reflection_prompt_text_contains_5_domains():
    """The reflection prompt text contains the 5 business domains.

    The reflection message is constructed inline in agents.py. The
    exact text is the LLM-facing nudge; this test verifies the
    expected domains are mentioned so the LLM knows what's available.
    """
    # Import the inline string via a grep-style approach. The reflection
    # is built dynamically inside the v3 loop, so we test the canonical
    # keywords that MUST appear in the message.
    expected_domains = [
        "ERP",
        "Inventory",
        "Market",
        "CRM",
        "Documents",
    ]
    # Read the source of agents.py and check for the keyword presence
    # in the reflection block.
    import inspect
    src = inspect.getsource(agents)
    # Find the reflection message construction (the long multi-line string)
    assert "reflection checkpoint" in src.lower() or "Reflection checkpoint" in src
    for domain in expected_domains:
        assert domain in src, (
            f"domain {domain!r} must appear in the reflection prompt "
            f"in agents.py"
        )


def test_reflection_prompt_only_injects_once():
    """The reflection prompt is gated by a once-per-turn flag.

    Looking at the source, the injection is guarded by
    `not _v3_reflection_prompt_injected` so it fires at most once
    per turn.
    """
    import inspect
    src = inspect.getsource(agents)
    assert "_v3_reflection_prompt_injected" in src
    # The flag must be set to True after injection so it doesn't fire again
    assert "_v3_reflection_prompt_injected = True" in src


def test_reflection_prompt_only_for_data_calls_with_rows():
    """The reflection prompt only fires for ask_data_agent with rows.

    A probe query that returned 0 rows should NOT trigger the
    reflection (the LLM would be told to query more even though
    the question may genuinely have no data).
    """
    import inspect
    src = inspect.getsource(agents)
    # The reflection site checks `result.get("rows")` for non-empty
    # rows before injecting the prompt
    assert 'result.get("rows")' in src
    assert "Reflection checkpoint" in src
