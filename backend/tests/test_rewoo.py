"""P1-4 ReWOO light-mode tests."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

import pytest

from app.config import settings
from app.database import Base, SessionLocal, engine
import app.models  # noqa: F401
from app.services.rewoo import (
    _parse_plan,
    rewoo_should_run,
    run_rewoo,
)


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


def _schema(name: str, desc: str = "does research") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": {}},
        },
    }


RESEARCH_TOOLS = [
    _schema("web_search", "search the web"),
    _schema("web_extract", "extract a page"),
    _schema("database_query", "run warehouse SQL"),
]


def test_gate_off_by_default(monkeypatch):
    monkeypatch.setattr(settings, "REWOO_ENABLED", False)
    assert rewoo_should_run("general_assistant", "a long enough research task", RESEARCH_TOOLS) is False


def test_gate_requires_research_catalog(monkeypatch):
    monkeypatch.setattr(settings, "REWOO_ENABLED", True)
    write_tools = [_schema("send_email", "sends email"), _schema("todo", "todo list")]
    assert rewoo_should_run("general_assistant", "a long enough task here", write_tools) is False
    assert rewoo_should_run("general_assistant", "a long enough research task", RESEARCH_TOOLS) is True


def test_gate_excludes_builders(monkeypatch):
    monkeypatch.setattr(settings, "REWOO_ENABLED", True)
    assert rewoo_should_run("workflow_builder", "a long enough research task", RESEARCH_TOOLS) is False


def test_gate_rejects_short_task(monkeypatch):
    monkeypatch.setattr(settings, "REWOO_ENABLED", True)
    assert rewoo_should_run("general_assistant", "short", RESEARCH_TOOLS) is False


def test_parse_plan_filters_unknown_tools():
    raw = (
        'Here is the plan: {"steps": ['
        '{"tool": "web_search", "args": {"q": "oil price"}, "purpose": "price"}, '
        '{"tool": "bogus_tool", "args": {}, "purpose": "nope"}]}'
    )
    steps = _parse_plan(raw, {"web_search"}, 6)
    assert len(steps) == 1
    assert steps[0]["tool"] == "web_search"
    assert steps[0]["args"] == {"q": "oil price"}


@pytest.mark.asyncio
async def test_run_rewoo_plan_execute_solve(db, monkeypatch):
    plan_json = (
        '{"steps": ['
        '{"tool": "web_search", "args": {"query": "oil price"}, "purpose": "find price"}]}'
    )

    async def _fake_llm(messages, tools, *, temperature=0.7, endpoint=None):
        if tools:
            return {"content": "should not happen"}
        if any("TOOL CATALOG" in m.get("content", "") for m in messages if isinstance(m, dict)):
            return {"content": plan_json}
        return {"content": "Oil is at $90/barrel [1]."}

    monkeypatch.setattr(
        "app.services.sub_agent_reliability.call_llm_with_reliability", _fake_llm)

    executed = {}

    async def _fake_exec(tool_name, arguments, db, user_id=None, **kw):
        executed["tool"] = tool_name
        executed["args"] = arguments
        return {"success": True, "results": ["$90"]}

    monkeypatch.setattr(
        "app.services.agent_tools.execute_tool_with_retry", _fake_exec)

    result = await run_rewoo(
        "What is the current oil price? Answer with the latest figure.",
        RESEARCH_TOOLS, db=db, user_id="u1")
    assert result["success"] is True
    assert result["mode"] == "rewoo"
    assert "$90" in result["response"]
    assert executed["tool"] == "web_search"
    assert executed["args"] == {"query": "oil price"}
    assert result["steps"][0]["ok"] is True


@pytest.mark.asyncio
async def test_run_rewoo_invalid_plan_fails_closed_to_caller(db, monkeypatch):
    async def _fake_llm(messages, tools, *, temperature=0.7, endpoint=None):
        return {"content": "no json here"}

    monkeypatch.setattr(
        "app.services.sub_agent_reliability.call_llm_with_reliability", _fake_llm)
    result = await run_rewoo(
        "a research task that is long enough for the gate",
        RESEARCH_TOOLS, db=db)
    assert result["success"] is False
    assert "plan" in result["error"]


@pytest.mark.asyncio
async def test_run_rewoo_worker_tool_error_recorded_not_raised(db, monkeypatch):
    plan_json = (
        '{"steps": [{"tool": "web_search", "args": {}, "purpose": "x"}]}'
    )

    async def _fake_llm(messages, tools, *, temperature=0.7, endpoint=None):
        if any("TOOL CATALOG" in m.get("content", "") for m in messages if isinstance(m, dict)):
            return {"content": plan_json}
        return {"content": "The evidence was insufficient."}

    monkeypatch.setattr(
        "app.services.sub_agent_reliability.call_llm_with_reliability", _fake_llm)

    async def _boom(*args, **kwargs):
        raise RuntimeError("tool down")

    monkeypatch.setattr(
        "app.services.agent_tools.execute_tool_with_retry", _boom)

    result = await run_rewoo(
        "a research task that is long enough for the gate",
        RESEARCH_TOOLS, db=db)
    assert result["success"] is True  # solver still runs on recorded errors
    assert result["steps"][0]["ok"] is False
