"""Tests for Phase 4 — deterministic deck router.

Covers the ``route_deck`` classification for each deck_type and the
design/pitch keyword signals (English + Chinese).  No LLM involved.
"""

from __future__ import annotations

from app.services.artifacts.deck_router import route_deck
from app.services.synexia.contracts import DeckPlan, SlidePlan


def _plan(deck_type: str = "data_report") -> DeckPlan:
    return DeckPlan(
        title="T",
        deck_type=deck_type,
        slides=[SlidePlan(layout="cover", title="C")],
    )


class TestRouteDeck:
    def test_plain_request_routes_sandbox_by_default(self):
        # A2 change: PPT_DESIGN_BY_DEFAULT=True routes plain pptx requests to
        # the sandbox HTML design renderer unless explicit plain intent is
        # signaled (previously this returned "structured").
        assert route_deck(_plan("data_report"), "make me a sales report") == "sandbox"

    def test_investor_deck_routes_sandbox(self):
        assert route_deck(_plan("investor_deck"), "") == "sandbox"

    def test_marketing_routes_sandbox(self):
        assert route_deck(_plan("marketing"), "") == "sandbox"

    def test_executive_brief_routes_sandbox_by_default(self):
        # A2 change: plain request now defaults to sandbox (was "structured").
        assert route_deck(_plan("executive_brief"), "summarize q3") == "sandbox"

    def test_beautiful_keyword_routes_sandbox(self):
        assert route_deck(_plan("data_report"), "make a beautiful deck") == "sandbox"

    def test_pitch_keyword_routes_sandbox(self):
        assert route_deck(_plan("data_report"), "pitch deck for investors") == "sandbox"

    def test_stunning_keyword_routes_sandbox(self):
        assert route_deck(_plan("data_report"), "make it stunning") == "sandbox"

    def test_chinese_keyword_routes_sandbox(self):
        assert route_deck(_plan("data_report"), "做一个精美的融资路演") == "sandbox"

    def test_no_plan_plain_request_routes_sandbox_by_default(self):
        # A2 change: plain request now defaults to sandbox (was "structured").
        assert route_deck(None, "give me the numbers") == "sandbox"

    def test_no_plan_design_keyword_routes_sandbox(self):
        assert route_deck(None, "make it stunning") == "sandbox"

    def test_report_keyword_is_not_a_sandbox_signal(self):
        # "report" is still a data signal, not a design signal — the sandbox
        # routing here comes from the A2 design-by-default, not the keyword.
        assert route_deck(_plan("data_report"), "make a report") == "sandbox"

    def test_case_insensitive_deck_type(self):
        assert route_deck(_plan("Investor_Deck"), "") == "sandbox"
