"""Tests for the deck-edit intent routing layer (PHASE 1C routing gate).

Covers the routing that surfaces the six deck-edit tools to the LLM:

* ``detect_deck_edit_intent`` maps EN/ZH edit phrasings to the correct tool
  and short-circuits regeneration phrasing (``regenerate`` / ``重新生成`` /
  ``from scratch`` ...) to ``None`` so the full regeneration path is kept.
* ``detect_deck_edit_routing`` is gated by ``DECK_EDIT_ROUTING_ENABLED``.
* ``pick_default_skill`` step 2.5 returns ``None`` for edit requests so a
  forced regeneration skill is never selected when routing is on.
* ``get_tools`` / ``_get_deck_edit_schemas`` inject exactly the six deck-edit
  schemas (incl. ``edit_slide``) when the flag is on, and inject nothing when
  it is off.
"""

from __future__ import annotations

import pytest

# Importing the handler module registers the six deck-edit tools in the
# registry (needed by the get_tools injection tests when this file runs
# alone — same pattern as test_deck_edit_tool.py).
import app.services.tool_handlers.deck_edit_tool  # noqa: F401
from app.config import settings
from app.services.agent_prompts import _get_deck_edit_schemas, get_tools
from app.services.synexia.default_skills import (
    DECK_EDIT_TOOL_NAMES,
    detect_deck_edit_routing,
    pick_default_skill,
)
from app.services.synexia.intent_router import detect_deck_edit_intent

EXPECTED_TOOL_NAMES = set(DECK_EDIT_TOOL_NAMES)


@pytest.fixture
def routing_on(monkeypatch):
    monkeypatch.setattr(settings, "DECK_EDIT_ROUTING_ENABLED", True)
    yield


@pytest.fixture
def routing_off(monkeypatch):
    monkeypatch.setattr(settings, "DECK_EDIT_ROUTING_ENABLED", False)
    yield


def _tool_names(schemas: list[dict]) -> set[str]:
    return {s["function"]["name"] for s in schemas}


class TestDetectDeckEditIntent:
    @pytest.mark.parametrize(
        "message,expected",
        [
            # EN edit phrasings
            ("remove slide 3", "remove_slide"),
            ("delete the page", "remove_slide"),
            ("reorder the slides", "reorder_slide"),
            ("rearrange the slides", "reorder_slide"),
            ("add a slide", "add_slide"),
            ("insert a new page", "add_slide"),
            ("update the chart", "update_chart"),
            ("modify the chart", "update_chart"),
            ("change the theme", "restyle_deck"),
            ("switch to a different theme", "restyle_deck"),
            ("edit slide 2", "edit_slide"),
            ("rewrite the page title", "edit_slide"),
            # ZH edit phrasings
            ("删除第2页", "remove_slide"),
            ("删掉第三页", "remove_slide"),
            ("调整顺序", "reorder_slide"),
            ("调换顺序", "reorder_slide"),
            ("添加一页", "add_slide"),
            ("新增一页", "add_slide"),
            ("修改图表", "update_chart"),
            ("更新图表", "update_chart"),
            ("换个主题", "restyle_deck"),
            ("换主题", "restyle_deck"),
            ("编辑第二页", "edit_slide"),
            ("修改第3页", "edit_slide"),
        ],
    )
    def test_maps_edit_phrasings(self, message, expected):
        assert detect_deck_edit_intent(message) == expected

    @pytest.mark.parametrize(
        "message",
        [
            # EN regeneration phrasing short-circuits to None
            "regenerate the deck",
            "regenerate",
            "redo it",
            "redo the deck",
            "start from scratch",
            "remake the deck",
            # ZH regeneration phrasing
            "重新生成",
            "重做",
            "重来",
            "从头生成",
        ],
    )
    def test_regeneration_short_circuits(self, message):
        assert detect_deck_edit_intent(message) is None

    def test_unrelated_or_empty_message_returns_none(self):
        assert detect_deck_edit_intent("what is the weather today") is None
        assert detect_deck_edit_intent("") is None
        assert detect_deck_edit_intent(None) is None


class TestDetectDeckEditRouting:
    def test_flag_off_returns_none_even_for_edit_request(self, routing_off):
        assert detect_deck_edit_routing("edit slide 2") is None
        assert detect_deck_edit_routing("修改图表") is None

    def test_flag_on_maps_edit_request(self, routing_on):
        assert detect_deck_edit_routing("edit slide 2") == "edit_slide"
        assert detect_deck_edit_routing("修改图表") == "update_chart"

    def test_flag_on_regeneration_still_none(self, routing_on):
        assert detect_deck_edit_routing("regenerate the deck") is None
        assert detect_deck_edit_routing("重新生成") is None


class TestPickDefaultSkillShortCircuit:
    def test_flag_on_returns_none_for_edit_request(self, routing_on):
        # Step 2.5: an edit request with routing on must NOT force a
        # regeneration skill — the chat loop surfaces the edit tools instead.
        assert pick_default_skill("edit slide 2") is None
        assert pick_default_skill("修改图表") is None

    def test_flag_off_still_returns_none(self, routing_off):
        # With the flag off, step 2.5 is skipped and the message falls through
        # to soft intent. "edit slide 2" matches no soft-intent pattern
        # (singular "slide" is absent from the pptx pattern), so the result
        # is None via the normal fallthrough.
        assert pick_default_skill("edit slide 2") is None

    def test_active_skill_override_takes_priority(self, routing_on):
        # Step 1 (user-picked skill) runs before step 2.5.
        assert (
            pick_default_skill("edit slide 2", active_skill={"name": "pptx"})
            is None
        )


class TestGetToolsDeckEditInjection:
    def test_deck_edit_schemas_absent_when_flag_off(self, routing_off):
        assert _get_deck_edit_schemas() == []

    def test_deck_edit_schemas_present_when_flag_on(self, routing_on):
        schemas = _get_deck_edit_schemas()
        names = _tool_names(schemas)
        assert EXPECTED_TOOL_NAMES <= names
        assert len(schemas) == len(EXPECTED_TOOL_NAMES)

    def test_get_tools_injects_deck_edit_when_flag_on(self, routing_on):
        schemas = get_tools(None, None, None)
        names = _tool_names(schemas)
        assert EXPECTED_TOOL_NAMES <= names
        # Exactly one copy of each deck-edit tool — no duplicate injection.
        for name in EXPECTED_TOOL_NAMES:
            assert sum(1 for s in schemas if s["function"]["name"] == name) == 1

    def test_get_tools_absent_when_flag_off(self, routing_off):
        schemas = get_tools(None, None, None)
        assert EXPECTED_TOOL_NAMES.isdisjoint(_tool_names(schemas))
