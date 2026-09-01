"""
Unit tests for the server-side turn-action router.

Locks in the contract that fixes the "LLM answers from training memory"
failure class:
  * precedence: ask_data_agent > create_artifact > web_extract > web_search
  * forcing happens on iteration 0 ONLY
  * a tool is never forced when the agent has not been granted it
  * URL beats time-sensitive when both match
  * doc intent maps to the right TurnAction.doc_format
"""

import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.turn_action import (
    GROUNDING_DOC_BLOCK,
    GROUNDING_URL_BLOCK,
    resolve_turn_action,
    grounding_block_for_message,
)
from app.services.agent_prompts import GROUNDING_REQUIRED_BLOCK

ALL_TOOLS = ["ask_data_agent", "create_artifact", "web_extract", "web_search"]
BOUND = {"bound_kb_ids": ["kb-1"]}
NO_BOUND: dict = {}


# ---------------------------------------------------------------------------
# precedence
# ---------------------------------------------------------------------------


def test_data_agent_wins_over_doc_intent():
    # The screenshot case: "create a pptx of sales" on a data-bound agent
    # must ask the data agent FIRST (file generation follows from its rows).
    action = resolve_turn_action(
        "create a pptx of quarterly sales",
        ALL_TOOLS, BOUND, is_data_question=True, iteration=0,
    )
    assert action.forced_tool == "ask_data_agent"
    assert action.doc_format is None


def test_doc_intent_wins_over_url_and_time_sensitive():
    action = resolve_turn_action(
        "create a docx from https://example.com with the latest news",
        ALL_TOOLS, NO_BOUND, is_data_question=False, iteration=0,
    )
    assert action.forced_tool == "create_artifact"
    assert action.doc_format == "docx"
    assert action.grounding_block == GROUNDING_DOC_BLOCK


def test_url_wins_over_time_sensitive():
    action = resolve_turn_action(
        "what is the latest news on https://example.com/feed",
        ALL_TOOLS, NO_BOUND, is_data_question=False, iteration=0,
    )
    assert action.forced_tool == "web_extract"
    assert action.grounding_block == GROUNDING_URL_BLOCK


def test_time_sensitive_forces_web_search():
    action = resolve_turn_action(
        "what is the latest news about AI chips",
        ALL_TOOLS, NO_BOUND, is_data_question=False, iteration=0,
    )
    assert action.forced_tool == "web_search"
    assert action.grounding_block == GROUNDING_REQUIRED_BLOCK


def test_general_chitchat_no_force():
    action = resolve_turn_action(
        "hello, how are you?",
        ALL_TOOLS, NO_BOUND, is_data_question=False, iteration=0,
    )
    assert action.forced_tool is None
    assert action.grounding_block == ""
    assert action.doc_format is None


# ---------------------------------------------------------------------------
# iteration-0-only
# ---------------------------------------------------------------------------


def test_no_forcing_after_iteration_zero():
    for it in (1, 2, 5):
        action = resolve_turn_action(
            "create a pptx of quarterly sales",
            ALL_TOOLS, BOUND, is_data_question=True, iteration=it,
        )
        assert action.forced_tool is None
        assert action.grounding_block == ""


# ---------------------------------------------------------------------------
# tool-presence guard
# ---------------------------------------------------------------------------


def test_data_agent_not_forced_when_tool_absent():
    action = resolve_turn_action(
        "how many orders last month?",
        ["web_search"], BOUND, is_data_question=True, iteration=0,
    )
    assert action.forced_tool != "ask_data_agent"


def test_data_agent_not_forced_without_bound_kbs():
    action = resolve_turn_action(
        "how many orders last month?",
        ALL_TOOLS, NO_BOUND, is_data_question=True, iteration=0,
    )
    assert action.forced_tool != "ask_data_agent"


def test_doc_intent_not_forced_when_create_artifact_absent():
    # Falls through to the next matching rule (none here) instead of
    # forcing a tool the agent does not have.
    action = resolve_turn_action(
        "create a pptx of quarterly sales",
        ["web_search"], NO_BOUND, is_data_question=False, iteration=0,
    )
    assert action.forced_tool is None


def test_url_not_forced_when_web_extract_absent():
    action = resolve_turn_action(
        "summarize https://example.com/article",
        ["web_search"], NO_BOUND, is_data_question=False, iteration=0,
    )
    # web_extract absent; the message is not time-sensitive → no force
    assert action.forced_tool is None


def test_tool_names_none_is_safe():
    action = resolve_turn_action(
        "create a pptx", None, None, is_data_question=False, iteration=0,
    )
    assert action.forced_tool is None


# ---------------------------------------------------------------------------
# doc-intent mapping
# ---------------------------------------------------------------------------


def test_doc_format_mapping():
    for phrase, fmt in (
        ("create a pptx deck", "pptx"),
        ("export this as a docx", "docx"),
        ("make a pdf report", "pdf"),
    ):
        action = resolve_turn_action(
            phrase, ALL_TOOLS, NO_BOUND, is_data_question=False, iteration=0,
        )
        assert action.forced_tool == "create_artifact"
        assert action.doc_format == fmt


def test_live_dashboard_intent_is_not_forced_to_create_artifact():
    action = resolve_turn_action(
        "make a weekly sales dashboard from the connected database",
        ALL_TOOLS, NO_BOUND, is_data_question=False, iteration=0,
    )
    assert action.forced_tool is None
    assert action.doc_format is None


def test_live_dashboard_intent_with_bound_data_is_not_forced_to_ask_data_agent():
    action = resolve_turn_action(
        "build a live sales dashboard from the connected database, not HTML",
        ALL_TOOLS, BOUND, is_data_question=True, iteration=0,
    )
    assert action.forced_tool is None
    assert action.doc_format is None


# ---------------------------------------------------------------------------
# grounding_block_for_message (soft nudge, no tool-presence guard)
# ---------------------------------------------------------------------------


def test_grounding_block_precedence_doc_over_url_over_time():
    assert grounding_block_for_message("make a pptx from https://x.com") == GROUNDING_DOC_BLOCK
    assert grounding_block_for_message("latest news on https://x.com") == GROUNDING_URL_BLOCK
    assert grounding_block_for_message("latest news about AI") == GROUNDING_REQUIRED_BLOCK
    assert grounding_block_for_message("build a live sales dashboard, not HTML") == ""
    assert grounding_block_for_message("hello there") == ""
    assert grounding_block_for_message(None) == ""
