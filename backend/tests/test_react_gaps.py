"""Tests for the IBM ReAct article gap fixes (2026-10-08).

#1 think tool (Thought step for non-reasoning models)
#2 text-scratchpad fallback parser
#3 signal-based loop-end nudge
#4 loop post-mortem → AgentMemory lessons
"""
import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

import json
from unittest.mock import patch

import pytest

from app.config import settings
from app.database import Base, engine, SessionLocal
import app.models  # noqa: F401
from app.models.agent_invocation import AgentInvocation
from app.models.agent_memory import AgentMemory
from app.routers.agents import _loop_end_nudge_message
from app.services.agent_loop.react_fallback import parse_react_text


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(settings, "REACT_SIGNAL_NUDGE_ENABLED", True)
    monkeypatch.setattr(settings, "REACT_SCRATCHPAD_FALLBACK_ENABLED", False)
    monkeypatch.setattr(settings, "LOOP_POSTMORTEM_ENABLED", False)
    yield


# ── #1 think tool ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_think_tool_records_and_never_fails():
    from app.services.tool_handlers.think_tool import _think
    from app.services.tool_registry import registry

    assert registry.get_entry("think") is not None
    result = await _think({"thought": "I should check the schema first"},
                          None, "u1", {"conversation_id": "conv-t1"})
    assert result["success"] is True
    result = await _think({"thought": ""}, None, "u1", None)
    assert result["success"] is False


# ── #2 scratchpad fallback parser ───────────────────────────────────────

def test_parse_react_text_json_input():
    content = (
        "Thought: I should search for the weather\n"
        'Action: web_search\n'
        'Action Input: {"query": "weather today"}\n'
        "Observation:"
    )
    call = parse_react_text(content)
    assert call is not None
    assert call["function"]["name"] == "web_search"
    assert json.loads(call["function"]["arguments"]) == {"query": "weather today"}
    assert call["type"] == "function" and call["id"]


def test_parse_react_text_bare_input():
    content = "Thought: need info\nAction: web_search\nAction Input: weather today"
    call = parse_react_text(content)
    assert call is not None
    assert json.loads(call["function"]["arguments"]) == {"input": "weather today"}


def test_parse_react_text_allowed_tools_gate():
    content = "Thought: x\nAction: write_file\nAction Input: {\"path\": \"/etc/passwd\"}"
    assert parse_react_text(content, allowed_tools=["web_search"]) is None
    assert parse_react_text(content, allowed_tools=["write_file"]) is not None


def test_parse_react_text_ignores_plain_answers():
    assert parse_react_text("The answer is 42 lots.") is None
    assert parse_react_text("") is None
    assert parse_react_text("Action was taken previously.") is None


def test_parse_react_text_fullwidth_colon():
    content = "Thought: x\nAction：web_search\nAction Input：{\"query\": \"y\"}"
    call = parse_react_text(content)
    assert call is not None and call["function"]["name"] == "web_search"


# ── #3 signal-based loop-end nudge ──────────────────────────────────────

def test_nudge_fires_on_grounding_tool_success():
    result = {"success": True, "rows": [{"n": 16143}]}
    msg = _loop_end_nudge_message("answer_from_database", result)
    assert msg is not None
    assert msg["role"] == "system"
    assert "LOOP SIGNAL" in msg["content"]


def test_nudge_fires_on_answer_text_without_rows():
    msg = _loop_end_nudge_message("ask_data_agent",
                                  {"success": True, "answer": "We have 16,143 lots."})
    assert msg is not None


def test_nudge_skips_non_grounding_tools():
    assert _loop_end_nudge_message("web_search", {"success": True, "rows": [[1]]}) is None
    assert _loop_end_nudge_message("think", {"success": True}) is None


def test_nudge_skips_failed_or_empty_results():
    assert _loop_end_nudge_message("ask_data_agent", {"success": False}) is None
    assert _loop_end_nudge_message("ask_data_agent", {"success": True}) is None


def test_nudge_respects_flag(monkeypatch):
    monkeypatch.setattr(settings, "REACT_SIGNAL_NUDGE_ENABLED", False)
    assert _loop_end_nudge_message(
        "answer_from_database", {"success": True, "rows": [[1]]}) is None


# ── #4 loop post-mortem ─────────────────────────────────────────────────

def _seed_failure(db, task, error):
    db.add(AgentInvocation(
        agent_app_id="app-1", invocation_type="sub_agent",
        input_message=task, status="failed", error_message=error,
        route="react",
    ))
    db.commit()


@pytest.mark.asyncio
async def test_postmortem_disabled_returns_zero(db):
    from app.services.loop_postmortem import run_loop_postmortem

    _seed_failure(db, "t", "e")
    summary = await run_loop_postmortem(db)
    assert summary == {"enabled": False, "lessons": 0}


@pytest.mark.asyncio
async def test_postmortem_distills_lessons_into_memory(db, monkeypatch):
    from app.services import loop_postmortem as lpm

    monkeypatch.setattr(settings, "LOOP_POSTMORTEM_ENABLED", True)
    _seed_failure(db, "sales report for July", "unknown column ORDER_DATE")
    _seed_failure(db, "inventory summary", "unknown column ORDER_DATE")

    async def fake_call_llm(**kw):
        return {"response": json.dumps([
            "Always call describe_schema before querying — assumed column "
            "names caused 2 failed loops."
        ])}

    with patch("app.services.llm_service.call_llm", side_effect=fake_call_llm):
        summary = await lpm.run_loop_postmortem(db)
    assert summary["lessons"] == 1
    # shared test DB: the disabled-flag test's seeded failure may also appear
    assert summary["failures"] >= 2

    row = db.query(AgentMemory).filter(
        AgentMemory.content.like("LESSON%")).first()
    assert row is not None
    assert "describe_schema" in row.content
    assert row.importance == 2

    # dedupe: a second identical run adds nothing
    with patch("app.services.llm_service.call_llm", side_effect=fake_call_llm):
        summary2 = await lpm.run_loop_postmortem(db)
    assert summary2["lessons"] == 0


@pytest.mark.asyncio
async def test_postmortem_llm_failure_is_non_fatal(db, monkeypatch):
    from app.services import loop_postmortem as lpm

    monkeypatch.setattr(settings, "LOOP_POSTMORTEM_ENABLED", True)
    _seed_failure(db, "t2", "boom")

    async def boom(**kw):
        raise RuntimeError("llm down")

    with patch("app.services.llm_service.call_llm", side_effect=boom):
        summary = await lpm.run_loop_postmortem(db)
    assert summary.get("lessons") == 0
