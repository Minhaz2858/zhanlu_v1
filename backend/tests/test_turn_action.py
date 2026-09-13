"""
Unit tests for the server-side turn-action resolver (advisory-only).

Phase A/B contract — no server-side tool forcing:
  * resolve_turn_action ALWAYS returns forced_tool=None (model picks)
  * grounding_block_for_message still returns soft prompt nudges only
  * grounding precedence remains: doc > URL > time-sensitive > none
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
# resolve_turn_action: no tool forcing (Phase A/B)
# ---------------------------------------------------------------------------


def test_no_tool_forcing_for_data_question():
    action = resolve_turn_action(
        "create a pptx of quarterly sales",
        ALL_TOOLS, BOUND, is_data_question=True, iteration=0,
    )
    assert action.forced_tool is None


def test_no_tool_forcing_for_doc_intent():
    action = resolve_turn_action(
        "create a docx from https://example.com with the latest news",
        ALL_TOOLS, NO_BOUND, is_data_question=False, iteration=0,
    )
    assert action.forced_tool is None


def test_no_tool_forcing_for_url():
    action = resolve_turn_action(
        "what is the latest news on https://example.com/feed",
        ALL_TOOLS, NO_BOUND, is_data_question=False, iteration=0,
    )
    assert action.forced_tool is None


def test_no_tool_forcing_for_time_sensitive():
    action = resolve_turn_action(
        "what is the latest news about AI chips",
        ALL_TOOLS, NO_BOUND, is_data_question=False, iteration=0,
    )
    assert action.forced_tool is None


def test_no_tool_forcing_for_general_chitchat():
    action = resolve_turn_action(
        "hello, how are you?",
        ALL_TOOLS, NO_BOUND, is_data_question=False, iteration=0,
    )
    assert action.forced_tool is None
    assert action.grounding_block == ""
    assert action.doc_format is None


def test_no_forcing_after_iteration_zero():
    for it in (1, 2, 5):
        action = resolve_turn_action(
            "create a pptx of quarterly sales",
            ALL_TOOLS, BOUND, is_data_question=True, iteration=it,
        )
        assert action.forced_tool is None


def test_no_forcing_for_weekly_summary_list_request():
    # The regression that started Phase A/B: "please list the weekly
    # summary…" must NOT be forced toward any file/data tool.
    action = resolve_turn_action(
        "Please list the weekly summary and forecast for C5C9 from December 24th to 30th",
        ALL_TOOLS, BOUND, is_data_question=False, iteration=0,
    )
    assert action.forced_tool is None
    assert action.doc_format is None


def test_tool_names_none_is_safe():
    action = resolve_turn_action(
        "create a pptx", None, None, is_data_question=False, iteration=0,
    )
    assert action.forced_tool is None


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


def test_list_request_never_forces_or_imposes_format():
    # The "list the weekly summary…" prompt is never forced toward a file
    # or data tool. The grounding block MAY carry an advisory time-aware
    # nudge (forecast + explicit dates read as time-sensitive), but that is
    # prompt text only — resolve_turn_action still returns no forced tool
    # and no doc_format.
    msg = (
        "Please list the weekly summary and forecast for C5C9 from December 24th to 30th"
    )
    action = resolve_turn_action(
        msg, ALL_TOOLS, NO_BOUND, is_data_question=False, iteration=0,
    )
    assert action.forced_tool is None
    assert action.doc_format is None
