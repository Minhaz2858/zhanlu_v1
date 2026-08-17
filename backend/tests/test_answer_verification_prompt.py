"""Regression tests for the Universal Answer Verification prompt block.

Guarantees:
1. ``_ANSWER_VERIFICATION_BLOCK`` contains ZERO domain-specific tokens
   (no table/column names, no product names, no vendor names) — the block
   must work identically for every agent and every data source.
2. The block is appended to EVERY agent path (system agents, generic
   fallback, user-created agents) ONLY when ``SELF_EVAL_REPLAN_ENABLED`` is
   on; flag-off output stays byte-identical (no block).
"""
import re
import types

import pytest

from app.config import settings
from app.services import agent_prompts

FORBIDDEN_TOKENS = [
    "erp_", "FNAME", "material_model", "c5_resin", "shipment_", "ecisco",
    "zhanlu", "deepseek", "openai", "F_PAEZ_JC", "orders", "inventory",
    "sales_", "product_id", "material_id", "contract_price",
]


def _prompt_block():
    return agent_prompts._ANSWER_VERIFICATION_BLOCK


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """Prompt block is flag-gated; most tests exercise the flag-on path."""
    monkeypatch.setattr(settings, "SELF_EVAL_REPLAN_ENABLED", True)
    yield
    monkeypatch.setattr(settings, "SELF_EVAL_REPLAN_ENABLED", False)


def _fake_agent_app():
    return types.SimpleNamespace(
        name="My Custom Agent",
        description="A test agent",
        prompt_identity="You are a helpful analyst.",
        prompt_boundary="Stay factual.",
        prompt_reasoning="Think step by step.",
        prompt_tools="Use tools when needed.",
        prompt_output="Be concise.",
        capabilities=["Database Query", "Web Search"],
    )


# ── zero hardcoding ─────────────────────────────────────────────────────


@pytest.mark.parametrize("token", FORBIDDEN_TOKENS)
def test_block_contains_no_domain_tokens(token):
    text = _prompt_block()
    assert token.lower() not in text.lower(), (
        f"_ANSWER_VERIFICATION_BLOCK must not hardcode domain token {token!r}"
    )


def test_block_mentions_all_four_checks():
    text = _prompt_block()
    assert "COMPLETENESS" in text
    assert "QUALITY" in text
    assert "SOURCE COVERAGE" in text
    assert "PLAUSIBILITY" in text


def test_block_mentions_source_types_without_names():
    """The re-plan table must be keyed by source TYPE, never by name."""
    text = _prompt_block()
    for hint in ("Database queries", "Documents", "Files", "APIs / web"):
        assert hint in text, f"missing source-type hint {hint!r}"


def test_block_forbids_metadata_and_vague_failure():
    text = _prompt_block()
    assert "metadata" in text.lower()
    assert "trouble putting it all together" in text.lower()


# ── flag-gated presence across all agent paths ─────────────────────────


def test_system_agent_prompt_has_block_when_flag_on():
    prompt = agent_prompts.get_system_prompt("general_assistant", user_message="hello")
    assert agent_prompts._ANSWER_VERIFICATION_BLOCK in prompt


def test_generic_fallback_has_block_when_flag_on():
    prompt = agent_prompts.get_system_prompt("totally_unknown_agent", user_message="hello")
    assert agent_prompts._ANSWER_VERIFICATION_BLOCK in prompt


def test_user_created_agent_has_block_when_flag_on():
    prompt = agent_prompts.get_system_prompt(
        "custom_agent_123", agent_app=_fake_agent_app(), user_message="hello"
    )
    assert agent_prompts._ANSWER_VERIFICATION_BLOCK in prompt


def test_data_agent_prompt_has_block_when_flag_on():
    prompt = agent_prompts.get_system_prompt("data_agent", user_message="hello")
    assert agent_prompts._ANSWER_VERIFICATION_BLOCK in prompt


def test_no_block_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "SELF_EVAL_REPLAN_ENABLED", False)
    for agent_name, app in [
        ("general_assistant", None),
        ("totally_unknown_agent", None),
        ("custom_agent_123", _fake_agent_app()),
    ]:
        prompt = agent_prompts.get_system_prompt(agent_name, agent_app=app, user_message="hello")
        assert agent_prompts._ANSWER_VERIFICATION_BLOCK not in prompt, (
            f"{agent_name} must NOT get the block when the flag is off"
        )


def test_runtime_context_block_still_present_with_verification_block():
    """Adding the verification block must not drop the date/time anchor."""
    prompt = agent_prompts.get_system_prompt("general_assistant", user_message="hello")
    assert agent_prompts._ANSWER_VERIFICATION_BLOCK in prompt
    assert "[CURRENT DATE & TIME]" in prompt
