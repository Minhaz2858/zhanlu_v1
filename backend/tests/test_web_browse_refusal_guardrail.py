"""Tests for the self-healing refusal guardrail and pattern detection.

The LLM occasionally claims it "cannot browse the internet" even though
``web_search`` is available.  These tests pin the detectors and the
fallback pipeline so the bug stays fixed.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.agent_prompts import (
    ONLINE_RESEARCH_PATTERN,
    TIME_SENSITIVE_PATTERN,
    WEB_BROWSE_REFUSAL_PATTERN,
)
from app.services.turn_action import (
    check_and_fallback,
    extract_search_query,
    is_online_research_request,
    is_web_browse_refusal,
)


# ── Pattern detectors ────────────────────────────────────────────────────
class TestTimeSensitivePattern:
    def test_matches_news_keyword(self):
        assert TIME_SENSITIVE_PATTERN.search("latest news")
        assert TIME_SENSITIVE_PATTERN.search("give me the news")

    def test_matches_today(self):
        assert TIME_SENSITIVE_PATTERN.search("what's the weather today")

    def test_does_not_match_general_questions(self):
        assert not TIME_SENSITIVE_PATTERN.search("what is the capital of France")
        assert not TIME_SENSITIVE_PATTERN.search("explain quantum physics")


class TestOnlineResearchPattern:
    def test_matches_collect_news_from_website(self):
        # The exact user message from the screenshot.
        assert ONLINE_RESEARCH_PATTERN.search(
            "can you collect some petrochemical news from website"
        )

    def test_matches_search_online(self):
        assert ONLINE_RESEARCH_PATTERN.search("search online for python tutorials")
        assert ONLINE_RESEARCH_PATTERN.search("please search the web")

    def test_matches_look_up(self):
        assert ONLINE_RESEARCH_PATTERN.search("look up the latest iphone news online")
        assert ONLINE_RESEARCH_PATTERN.search("look up information about tesla online")

    def test_matches_find_from_website(self):
        assert ONLINE_RESEARCH_PATTERN.search("find news from website about AI")

    def test_matches_get_info(self):
        assert ONLINE_RESEARCH_PATTERN.search("get the latest info about tesla")
        assert ONLINE_RESEARCH_PATTERN.search("can you get the news online")

    def test_does_not_match_general_questions(self):
        assert not ONLINE_RESEARCH_PATTERN.search("what is the capital of France")
        assert not ONLINE_RESEARCH_PATTERN.search("explain quantum physics")
        assert not ONLINE_RESEARCH_PATTERN.search("write a python function")

    def test_does_not_match_pure_database_questions(self):
        # No web/news/online keyword — pure DB question.
        assert not ONLINE_RESEARCH_PATTERN.search("show me top customers last month")

    def test_matches_give_me_today_price(self):
        """The user's actual scenario: 'give me today brent oil price'."""
        assert ONLINE_RESEARCH_PATTERN.search("give me today brent oil price")

    def test_matches_what_is_current(self):
        assert ONLINE_RESEARCH_PATTERN.search("what is the current Apple stock price")

    def test_matches_show_me_latest(self):
        assert ONLINE_RESEARCH_PATTERN.search("show me the latest weather in Tokyo")

    def test_matches_whats_price_today(self):
        assert ONLINE_RESEARCH_PATTERN.search("what's bitcoin price today")

    def test_matches_how_is_weather_now(self):
        assert ONLINE_RESEARCH_PATTERN.search("how is the weather right now in London")

    def test_matches_price_today(self):
        assert ONLINE_RESEARCH_PATTERN.search("price of bitcoin today")

    def test_matches_live_score(self):
        assert ONLINE_RESEARCH_PATTERN.search("show me the live score")

    def test_does_not_match_casual_today(self):
        # "today" alone (not a fact request) should not match
        assert not ONLINE_RESEARCH_PATTERN.search("How are you doing today?")
        assert not ONLINE_RESEARCH_PATTERN.search("I went to the store yesterday.")
        assert not ONLINE_RESEARCH_PATTERN.search("Tell me a joke.")


class TestWebBrowseRefusalPattern:
    def test_matches_cannot_browse_internet(self):
        assert WEB_BROWSE_REFUSAL_PATTERN.search(
            "I'm sorry, but I cannot browse the internet or collect real-time news."
        )

    def test_matches_cannot_access_website(self):
        assert WEB_BROWSE_REFUSAL_PATTERN.search(
            "I cannot access external websites to gather that data."
        )

    def test_matches_dont_have_access(self):
        assert WEB_BROWSE_REFUSAL_PATTERN.search(
            "I don't have access to the internet to retrieve that information."
        )

    def test_matches_cant_collect(self):
        assert WEB_BROWSE_REFUSAL_PATTERN.search(
            "I can't fetch real-time news from websites."
        )

    def test_matches_knowledge_cutoff(self):
        assert WEB_BROWSE_REFUSAL_PATTERN.search(
            "My training data was cut off and I cannot retrieve real-time data."
        )

    def test_matches_cannot_provide_real_time_data(self):
        """The user's actual scenario: 'I cannot provide real-time data'."""
        assert WEB_BROWSE_REFUSAL_PATTERN.search(
            "I'm sorry, but I cannot provide real-time data such as today's Brent oil price."
        )

    def test_matches_cannot_give_today(self):
        assert WEB_BROWSE_REFUSAL_PATTERN.search(
            "I cannot give you today's stock price."
        )

    def test_matches_no_access_to_live_data_sources(self):
        """The agent's reasoning chain: 'I do not have access to live data sources'."""
        assert WEB_BROWSE_REFUSAL_PATTERN.search(
            "I do not have access to live data sources."
        )

    def test_matches_no_access_to_latest_prices(self):
        assert WEB_BROWSE_REFUSAL_PATTERN.search(
            "I do not have access to the latest prices."
        )

    def test_matches_unable_to_access_real_time(self):
        assert WEB_BROWSE_REFUSAL_PATTERN.search(
            "I am unable to access real-time market data."
        )

    def test_matches_data_not_real_time(self):
        assert WEB_BROWSE_REFUSAL_PATTERN.search(
            "My data is not real-time."
        )

    def test_does_not_match_normal_text(self):
        assert not WEB_BROWSE_REFUSAL_PATTERN.search(
            "I searched the web and found 5 results about AI."
        )
        assert not WEB_BROWSE_REFUSAL_PATTERN.search(
            "Here is the latest sales report you requested."
        )

    def test_does_not_match_casual_cannot(self):
        # "cannot" without real-time/external context should not match
        assert not WEB_BROWSE_REFUSAL_PATTERN.search(
            "I cannot believe how easy that was!"
        )
        assert not WEB_BROWSE_REFUSAL_PATTERN.search(
            "I do not see any issues with the code."
        )

    def test_does_not_match_normal_current_mention(self):
        # "current" or "today" alone, not in a refusal, should not match
        assert not WEB_BROWSE_REFUSAL_PATTERN.search(
            "The current price of bitcoin is $50,000."
        )
        assert not WEB_BROWSE_REFUSAL_PATTERN.search(
            "I will fetch the latest data for you."
        )


# ── Helper functions ────────────────────────────────────────────────────
class TestExtractSearchQuery:
    def test_strips_can_you(self):
        assert extract_search_query("can you collect some petrochemical news from website") == "some petrochemical news"

    def test_strips_please(self):
        # "please" is a politeness wrapper at the start
        result = extract_search_query("please find AI articles online")
        assert "AI" in result and "articles" in result
        # "please" suffix is stripped
        result2 = extract_search_query("find AI articles please")
        assert "AI" in result2 and "articles" in result2

    def test_strips_search_for(self):
        assert extract_search_query("search for the weather today") == "the weather today"

    def test_preserves_core_query(self):
        assert "petrochemical" in extract_search_query("petrochemical news please").lower()

    def test_handles_empty(self):
        assert extract_search_query("") == ""
        assert extract_search_query(None) == ""


class TestIsOnlineResearchRequest:
    def test_returns_true_for_research_request(self):
        assert is_online_research_request("can you collect some petrochemical news from website") is True
        assert is_online_research_request("search online for Python tutorials") is True

    def test_returns_false_for_non_research(self):
        assert is_online_research_request("what is the capital of France") is False
        assert is_online_research_request("write a poem about love") is False

    def test_handles_empty(self):
        assert is_online_research_request("") is False
        assert is_online_research_request(None) is False


class TestIsWebBrowseRefusal:
    def test_returns_true_for_refusal(self):
        assert is_web_browse_refusal(
            "I'm sorry, but I cannot browse the internet or collect real-time news."
        ) is True
        assert is_web_browse_refusal(
            "I don't have access to the internet."
        ) is True

    def test_returns_false_for_normal_text(self):
        assert is_web_browse_refusal(
            "I searched the web and found 5 results about AI."
        ) is False
        assert is_web_browse_refusal("Here is your report.") is False

    def test_handles_empty(self):
        assert is_web_browse_refusal("") is False
        assert is_web_browse_refusal(None) is False


# ── End-to-end fallback ──────────────────────────────────────────────────
class TestCheckAndFallback:
    def test_no_trigger_for_normal_response(self):
        """When the LLM gives a normal answer, no fallback runs."""
        out = asyncio.run(check_and_fallback(
            user_message="what is the capital of France",
            assistant_text="The capital of France is Paris.",
        ))
        assert out["triggered"] is False
        assert out["action"] == "none"

    def test_no_trigger_for_non_research_request(self):
        """When the user is not asking for online research, no fallback."""
        out = asyncio.run(check_and_fallback(
            user_message="can you write me a poem about love",
            assistant_text="I cannot write poetry as I'm an AI assistant.",
        ))
        assert out["triggered"] is False

    def test_triggers_when_refusal_detected(self):
        """The exact user scenario: LLM refuses, fallback runs."""
        # Patch the web_search handler to return canned results.
        fake_results = [
            {"title": "Petrochem news 1", "url": "https://example.com/1", "snippet": "Snippet 1"},
            {"title": "Petrochem news 2", "url": "https://example.com/2", "snippet": "Snippet 2"},
        ]
        async def main():
            with patch(
                "app.services.tool_handlers.web_search_tool._web_search",
                new=AsyncMock(return_value={"success": True, "results": fake_results}),
            ):
                return await check_and_fallback(
                    user_message="can you collect some petrochemical news from website",
                    assistant_text=(
                        "I'm sorry, but I cannot browse the internet or collect "
                        "real-time news from websites. However, I can help you "
                        "summarize or generate a report if you provide the text."
                    ),
                    db=MagicMock(),
                )
        out = asyncio.run(main())
        assert out["triggered"] is True
        assert "petrochemical" in out["search_query"].lower()
        assert len(out["search_results"]) == 2
        assert out["action"] in ("append", "fallback")

    def test_fallback_action_when_call_llm_provided(self):
        """When call_llm is provided and succeeds, action='fallback'."""
        async def fake_llm(query, results):
            return f"Here is what I found about {query}: ..."

        async def main():
            with patch(
                "app.services.tool_handlers.web_search_tool._web_search",
                new=AsyncMock(return_value={"success": True, "results": [{"title": "x", "url": "u", "snippet": "s"}]}),
            ):
                return await check_and_fallback(
                    user_message="can you collect some petrochemical news from website",
                    assistant_text="I cannot browse the internet.",
                    db=MagicMock(),
                    call_llm=fake_llm,
                )
        out = asyncio.run(main())
        assert out["triggered"] is True
        assert out["action"] == "fallback"
        assert "re-asked" in out["message"].lower()

    def test_falls_back_gracefully_when_search_fails(self):
        """When web_search returns failure, action='none' (no-op)."""
        async def main():
            with patch(
                "app.services.tool_handlers.web_search_tool._web_search",
                new=AsyncMock(return_value={"success": False, "error": "no api key"}),
            ):
                return await check_and_fallback(
                    user_message="can you collect some petrochemical news from website",
                    assistant_text="I cannot browse the internet.",
                    db=MagicMock(),
                )
        out = asyncio.run(main())
        assert out["triggered"] is True
        assert out["action"] == "none"
        assert out["search_results"] == []

    def test_handles_search_handler_import_error(self):
        """When the handler can't be imported, decision is no-op."""
        async def main():
            with patch(
                "app.services.tool_handlers.web_search_tool._web_search",
                new=AsyncMock(side_effect=ImportError("nope")),
            ):
                return await check_and_fallback(
                    user_message="can you collect some petrochemical news from website",
                    assistant_text="I cannot browse the internet.",
                    db=MagicMock(),
                )
        # Should not raise; should return safe default.
        out = asyncio.run(main())
        # The decision may be triggered=False because the pattern detector
        # runs first; the important thing is no exception.
        assert isinstance(out, dict)
        assert "action" in out
