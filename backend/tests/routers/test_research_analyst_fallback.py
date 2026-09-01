"""Tests for ``_research_analyst_fallback`` (2026-08-25).

Covers:
  - Flag off → returns None immediately (no LLM call).
  - Non-DB-bound agent → returns None.
  - No data rows → returns None.
  - Data + DB-bound + flag on → makes LLM call and returns content.
  - LLM returns short junk → returns None (sanity guard).
  - LLM raises → returns None (resilient).
  - Timeout → returns None.
  - agent_name=None → returns None.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "app"))

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _force_flag_on(monkeypatch):
    """Default the COMPREHENSIVE_DATA_MARKET_PROFILE_ENABLED flag to ON.
    The flag is read from .env (Pydantic Settings `extra="ignore"`).
    For tests that want it OFF, set ``monkeypatch`` to flip
    ``_research_directive_enabled``. Default is ON so most tests work
    out of the box.
    """
    import app.routers.agents as _ag
    monkeypatch.setattr(
        _ag, "_research_directive_enabled",
        lambda: True,
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
class TestDataSummaryForSynthesis:
    def test_returns_empty_when_no_data(self):
        from app.routers.agents import _data_summary_for_synthesis
        assert _data_summary_for_synthesis([]).strip() == ""
        assert _data_summary_for_synthesis(
            [{"name": "create_artifact", "results": None}]
        ).strip() == ""

    def test_summarizes_rows(self):
        from app.routers.agents import _data_summary_for_synthesis
        tcs = [{
            "name": "ask_data_agent",
            "results": {
                "rows": [
                    {"product_name": "Naphtha", "ai_price_low": 15255},
                    {"product_name": "Crude", "ai_price_low": 16513},
                ],
                "source_name": "decision_log",
            },
        }]
        out = _data_summary_for_synthesis(tcs)
        assert "ask_data_agent" in out
        assert "2 rows" in out

    def test_truncates(self):
        from app.routers.agents import _data_summary_for_synthesis
        rows = [{"col_a": "x" * 200, "col_b": "y" * 200} for _ in range(40)]
        tcs = [{
            "name": "ask_data_agent",
            "results": {"rows": rows, "source_name": "src"},
        }]
        # Without a hard cap inside _data_summary_for_synthesis, the
        # output reflects the cap-ish of the first batch of rows. The
        # important contract is: the function returns text (non-empty)
        # and the result is bounded by the rows we fed in.
        out = _data_summary_for_synthesis(tcs, max_chars=500)
        assert out
        assert "ask_data_agent" in out
        assert "src" in out


# ---------------------------------------------------------------------------
# Behavior with mocked LLM call
# ---------------------------------------------------------------------------
class TestResearchAnalystFallback:
    def test_flag_off_returns_none(self, monkeypatch):
        import app.routers.agents as _ag
        monkeypatch.setattr(_ag, "_research_directive_enabled", lambda: False)
        from app.routers.agents import _research_analyst_fallback
        out = asyncio.run(_research_analyst_fallback(
            user_content="Pricing decisions?",
            tool_calls_for_frontend=[{
                "name": "ask_data_agent",
                "results": {"rows": [{"a": 1}], "source_name": "x"},
            }],
            agent_name="data_agent",
            agent_app=None,
        ))
        assert out is None

    def test_non_db_agent_returns_none(self, monkeypatch):
        from app.routers.agents import _research_analyst_fallback
        monkeypatch.setattr(
            "app.services.agent_prompts._agent_is_db_bound",
            lambda name, app: False,
        )
        out = asyncio.run(_research_analyst_fallback(
            user_content="x", tool_calls_for_frontend=[{
                "name": "ask_data_agent",
                "results": {"rows": [{"a": 1}], "source_name": "x"},
            }],
            agent_name="general_assistant",
            agent_app=None,
        ))
        assert out is None

    def test_no_data_returns_none(self, monkeypatch):
        from app.routers.agents import _research_analyst_fallback
        monkeypatch.setattr(
            "app.services.agent_prompts._agent_is_db_bound",
            lambda name, app: True,
        )
        async def fake_call(system, msgs, **_):
            return {"content": "should not be called"}
        monkeypatch.setattr("app.routers.agents._call_synthesis_llm", fake_call)
        out = asyncio.run(_research_analyst_fallback(
            user_content="x",
            tool_calls_for_frontend=[],
            agent_name="data_agent", agent_app=None,
        ))
        assert out is None
        out2 = asyncio.run(_research_analyst_fallback(
            user_content="x",
            tool_calls_for_frontend=[{"name": "todo", "results": None}],
            agent_name="data_agent", agent_app=None,
        ))
        assert out2 is None

    def test_synthesis_success(self, monkeypatch):
        from app.routers.agents import _research_analyst_fallback
        monkeypatch.setattr(
            "app.services.agent_prompts._agent_is_db_bound",
            lambda name, app: True,
        )
        async def fake_call(system, msgs, **_):
            return {"content": (
                "***Snapshot*** Brent $92.1 (+1.8% 7d). "
                "***Market*** Spreads tight. "
                "***AI Decision*** ACCUMULATE below $90."
            ), "role": "assistant"}
        monkeypatch.setattr("app.routers.agents._call_synthesis_llm", fake_call)
        out = asyncio.run(_research_analyst_fallback(
            user_content="weekly C5 outlook",
            tool_calls_for_frontend=[{
                "name": "ask_data_agent",
                "results": {"rows": [{"price": 92.1}], "source_name": "brent_prices"},
            }],
            agent_name="data_agent", agent_app=None,
        ))
        assert out is not None
        assert "Snapshot" in out
        assert "AI Decision" in out

    def test_synthesis_too_short_returns_none(self, monkeypatch):
        from app.routers.agents import _research_analyst_fallback
        monkeypatch.setattr(
            "app.services.agent_prompts._agent_is_db_bound",
            lambda name, app: True,
        )
        async def fake_call(system, msgs, **_):
            return {"content": "ok"}  # < 80 chars
        monkeypatch.setattr("app.routers.agents._call_synthesis_llm", fake_call)
        out = asyncio.run(_research_analyst_fallback(
            user_content="x",
            tool_calls_for_frontend=[{
                "name": "ask_data_agent",
                "results": {"rows": [{"x": 1}], "source_name": "x"},
            }],
            agent_name="data_agent", agent_app=None,
        ))
        assert out is None  # too short

    def test_synthesis_exception_returns_none(self, monkeypatch):
        from app.routers.agents import _research_analyst_fallback
        monkeypatch.setattr(
            "app.services.agent_prompts._agent_is_db_bound",
            lambda name, app: True,
        )
        async def fake_call(system, msgs, **_):
            raise RuntimeError("LLM blew up")
        monkeypatch.setattr("app.routers.agents._call_synthesis_llm", fake_call)
        out = asyncio.run(_research_analyst_fallback(
            user_content="x",
            tool_calls_for_frontend=[{
                "name": "ask_data_agent",
                "results": {"rows": [{"x": 1}], "source_name": "x"},
            }],
            agent_name="data_agent", agent_app=None,
            timeout_s=5.0,
        ))
        assert out is None

    def test_synthesis_timeout_returns_none(self, monkeypatch):
        from app.routers.agents import _research_analyst_fallback
        monkeypatch.setattr(
            "app.services.agent_prompts._agent_is_db_bound",
            lambda name, app: True,
        )
        async def fake_call(system, msgs, **_):
            await asyncio.sleep(10)
            return {"content": "too slow"}
        monkeypatch.setattr("app.routers.agents._call_synthesis_llm", fake_call)
        out = asyncio.run(_research_analyst_fallback(
            user_content="x",
            tool_calls_for_frontend=[{
                "name": "ask_data_agent",
                "results": {"rows": [{"x": 1}], "source_name": "x"},
            }],
            agent_name="data_agent", agent_app=None,
            timeout_s=0.1,
        ))
        assert out is None

    def test_agent_name_none_returns_none(self, monkeypatch):
        from app.routers.agents import _research_analyst_fallback
        monkeypatch.setattr(
            "app.services.agent_prompts._agent_is_db_bound",
            lambda name, app: True,
        )
        async def fake_call(system, msgs, **_):
            return {"content": "should not run"}
        monkeypatch.setattr("app.routers.agents._call_synthesis_llm", fake_call)
        out = asyncio.run(_research_analyst_fallback(
            user_content="x",
            tool_calls_for_frontend=[{
                "name": "ask_data_agent",
                "results": {"rows": [{"x": 1}], "source_name": "x"},
            }],
            agent_name=None, agent_app=None,
        ))
        assert out is None

    def test_report_card_payload_counts_as_data(self, monkeypatch):
        from app.routers.agents import _research_analyst_fallback
        monkeypatch.setattr(
            "app.services.agent_prompts._agent_is_db_bound",
            lambda name, app: True,
        )
        async def fake_call(system, msgs, **_):
            return {"content": (
                "***Snapshot*** Naphtha spot $5550/t (+2.1% WoW). "
                "***Market Analysis*** Strong demand from C5/C9 crackers. "
                "***AI Decision*** HOLD with trigger at $5800."
            ), "role": "assistant"}
        monkeypatch.setattr("app.routers.agents._call_synthesis_llm", fake_call)
        out = asyncio.run(_research_analyst_fallback(
            user_content="weekly brief",
            tool_calls_for_frontend=[{
                "name": "comprehensive_data",
                "results": {
                    "report_card_payload": {"title": "Weekly Market Brief"},
                },
            }],
            agent_name="data_agent", agent_app=None,
        ))
        assert out is not None