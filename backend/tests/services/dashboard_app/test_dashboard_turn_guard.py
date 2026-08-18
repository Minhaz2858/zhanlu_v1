"""T7: data-contract confirmation gate + prompt hard-rule tests.

The gate blocks dashboard builds when the data contract is unconfirmed
(ambiguous request, no schema grounding, no user approval), so the agent can
never build on invented table/column names. The prompt hard rule is asserted
separately against the default skills block.
"""
import pytest

from app.config import settings
from app.services.agent_prompts import _DEFAULT_SKILLS_BLOCK
from app.services.dashboard_turn_guard import (
    contract_confirmation_needed,
    is_live_dashboard_request,
    mentions_concrete_entities,
)


@pytest.fixture
def fullstack_on(monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", True)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", False)
    yield


def _needed(user_content, tool_names=()):
    tcf = [{"name": n} for n in tool_names]
    return contract_confirmation_needed(user_content, tcf)


# ── mentions_concrete_entities ─────────────────────────────────────────────

def test_mentions_concrete_entities_snake_case():
    assert mentions_concrete_entities("dashboard of erp_product_sales_details")


def test_mentions_concrete_entities_uppercase_acronym():
    assert mentions_concrete_entities("show me FNAME trends")


def test_mentions_concrete_entities_plain_words_false():
    assert not mentions_concrete_entities("make me a sales dashboard")


def test_mentions_concrete_entities_empty_false():
    assert not mentions_concrete_entities("")


# ── contract_confirmation_needed ───────────────────────────────────────────

def test_ambiguous_request_needs_confirmation(fullstack_on):
    """Vague dashboard request + no schema grounding + no approval → gate."""
    assert _needed("make me a dashboard of our sales") is True


def test_ambiguous_request_zh_needs_confirmation(fullstack_on):
    assert _needed("帮我做一个销售仪表盘") is True


def test_schema_inspection_grounds_the_agent(fullstack_on):
    """describe_schema ran this turn → the agent is grounded, gate passes."""
    assert _needed(
        "make me a dashboard of our sales",
        tool_names=["describe_schema"],
    ) is False


def test_inspect_data_source_grounds_too(fullstack_on):
    assert _needed(
        "make me a dashboard",
        tool_names=["inspect_data_source"],
    ) is False


def test_user_approval_passes_gate(fullstack_on):
    assert _needed("yes, build it") is False


def test_user_approval_zh_passes_gate(fullstack_on):
    assert _needed("可以，构建吧") is False


def test_concrete_request_passes_gate(fullstack_on):
    """User names real tables/columns → the agent inspects and builds."""
    assert _needed("dashboard of erp_product_sales_details showing FNAME") is False


def test_non_dashboard_request_passes(fullstack_on):
    assert _needed("please write a report on our inventory") is False


def test_empty_content_passes(fullstack_on):
    assert _needed("") is False
    assert _needed(None) is False


def test_build_already_attempted_passes(fullstack_on):
    assert _needed(
        "make me a dashboard",
        tool_names=["create_fullstack_dashboard"],
    ) is False


def test_gate_inert_when_flags_off(monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", False)
    assert _needed("make me a dashboard") is False


def test_gate_active_with_legacy_tool(monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", True)
    assert _needed("make me a dashboard") is True
    assert _needed(
        "make me a dashboard",
        tool_names=["describe_schema"],
    ) is False


# ── prompt hard rule ────────────────────────────────────────────────────────

def test_data_contract_hard_rule_present_in_prompt():
    assert "DATA-CONTRACT CONFIRMATION" in _DEFAULT_SKILLS_BLOCK
    assert "NEVER invent, guess, or fabricate table or column names" in _DEFAULT_SKILLS_BLOCK
    assert "ask ONE short clarifying question" in _DEFAULT_SKILLS_BLOCK
    assert "An honest clarification beats a dashboard built on fabricated data" in _DEFAULT_SKILLS_BLOCK


# ── no regression on existing request detection ────────────────────────────

def test_is_live_dashboard_request_still_works():
    assert is_live_dashboard_request("build me a live dashboard") is True
    assert is_live_dashboard_request("给我做一个数据看板") is True
    assert is_live_dashboard_request("summarize the weekly report") is False
