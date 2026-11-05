"""Milestone final-acceptance tests (2026-11-01): invocation-boundary
enforcement of the agent tool policy.

Guarantees under test:
1. A strict AgentApp child CANNOT invoke a tool outside its allowlist by
   guessing its name — execute_tool denies it.
2. A tool listed in disabled_tools is denied even when also enabled, even
   always-on evidence tools, even for non-strict agents.
3. Universal access for non-strict agents is preserved (no regression).
4. search_tools is invocable through execute_tool (arity fix) and never
   advertises tools forbidden by the caller's policy.

No LLM calls — fully deterministic.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

import asyncio

import pytest

from app.config import settings
from app.database import Base, engine, SessionLocal
import app.models  # noqa: F401
from app.models.agent_app import AgentApp
from app.services.tool_registry import registry
from app.services import agent_tool_policy as atp


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


@pytest.fixture
def _dummy_tool():
    """A registered tool the strict allowlist does NOT include."""
    def _handler(args, db=None, user_id=None, context=None):
        return {"success": True, "called": "guessed_tool_ok"}

    registry.register(
        name="guessed_secret_tool",
        schema={"type": "function", "function": {
            "name": "guessed_secret_tool",
            "description": "not in any allowlist",
            "parameters": {"type": "object", "properties": {}},
        }},
        handler=_handler,
        category="test",
    )
    yield "guessed_secret_tool"
    registry.deregister("guessed_secret_tool")


def _make_app(db, **tool_config):
    app = AgentApp(
        name=f"Policy Gate Agent {tool_config.get('strict_tools')}",
        description="policy gate test",
        status="published",
        tool_config=tool_config,
        org_id="default-org",
        app_id="default-app",
    )
    db.add(app)
    db.commit()
    yield app
    db.delete(app)
    db.commit()


@pytest.fixture
def strict_app(db):
    yield from _make_app(
        db, enabled_tools=["provide_evidence"], strict_tools=True,
    )


@pytest.fixture
def deny_app(db):
    yield from _make_app(
        db,
        enabled_tools=["guessed_secret_tool", "provide_evidence"],
        disabled_tools=["guessed_secret_tool"],
        strict_tools=False,
    )


@pytest.fixture
def open_app(db):
    yield from _make_app(db, enabled_tools=["query_database"])


def _ctx(app):
    return {"agent_app_id": app.id, "agent_name": app.name}


# ── 1. strict allowlist at the invocation boundary ──────────────────────

def test_strict_agent_cannot_guess_tool(db, strict_app, _dummy_tool):
    result = asyncio.run(_exec(
        db, "guessed_secret_tool", {}, _ctx(strict_app)))
    assert result["success"] is False
    assert "strict tool" in (result.get("error") or "").lower()


def test_strict_agent_allows_allowlisted_tool(db, strict_app):
    # provide_evidence is in the allowlist — the policy gate must NOT deny
    # it (the tool itself may fail for data reasons, but not with a policy
    # denial).
    result = asyncio.run(_exec(db, "provide_evidence", {}, _ctx(strict_app)))
    err = (result.get("error") or "")
    assert "strict tool" not in err.lower(), err


def test_strict_agent_still_gets_always_on_evidence_tools(db, strict_app):
    # request_evidence is NOT in enabled_tools but is always-on: the policy
    # gate admits it (matching the assembly contract).
    result = asyncio.run(_exec(db, "request_evidence", {}, _ctx(strict_app)))
    err = (result.get("error") or "")
    assert "strict tool" not in err.lower(), err


# ── 2. disabled_tools hard deny ─────────────────────────────────────────

def test_disabled_tool_denied_even_when_enabled(db, deny_app, _dummy_tool):
    result = asyncio.run(_exec(db, "guessed_secret_tool", {}, _ctx(deny_app)))
    assert result["success"] is False
    assert "disabled" in (result.get("error") or "").lower()


def test_disabled_always_on_tool_denied(db, strict_app):
    strict_app.tool_config = {
        "enabled_tools": [], "strict_tools": True,
        "disabled_tools": ["request_evidence"],
    }
    db.commit()
    result = asyncio.run(_exec(db, "request_evidence", {}, _ctx(strict_app)))
    assert result["success"] is False
    assert "disabled" in (result.get("error") or "").lower()


# ── 3. universal access preserved for non-strict agents ────────────────

def test_non_strict_agent_can_invoke_discovered_tool(db, open_app, _dummy_tool):
    result = asyncio.run(_exec(db, "guessed_secret_tool", {}, _ctx(open_app)))
    assert result.get("success") is True, result
    assert result.get("called") == "guessed_tool_ok"


def test_no_agent_app_no_gate(db, _dummy_tool):
    result = asyncio.run(_exec(db, "guessed_secret_tool", {}, {}))
    assert result.get("success") is True, result


# ── 4. search_tools arity + discovery honesty ───────────────────────────

def test_search_tools_handler_signature_matches_invocation_contract(db):
    from app.services.tool_search import handle_search_tools

    # execute_tool dispatches sync handlers as (args, db, user_id) — the
    # old 1-arg signature crashed on every governed invocation.
    result = handle_search_tools({"query": "query"}, db, None)
    assert isinstance(result, dict)
    assert result.get("success") is True


def test_search_tools_never_advertises_disabled_tools(db, deny_app, _dummy_tool):
    from app.services.tool_search import handle_search_tools

    result = handle_search_tools(
        {"query": "guessed", "limit": 10}, db, None, context=_ctx(deny_app),
    )
    names = [m.get("name") for m in result.get("matches", [])]
    assert "guessed_secret_tool" not in names


def test_policy_unit_contract():
    policy = {"strict": True, "enabled": {"a"}, "disabled": {"b", "provide_evidence"}}
    assert atp.policy_denies(policy, "b")
    assert atp.policy_denies(policy, "provide_evidence")  # deny wins over always-on
    assert atp.policy_denies(policy, "c")  # strict: not in allowlist
    assert atp.policy_denies(policy, "a") is None
    assert atp.policy_denies({"strict": False, "enabled": set(), "disabled": set()}, "x") is None
    assert atp.policy_denies(None, "x") is None


# ── helper ───────────────────────────────────────────────────────────────

def _exec(db, tool_name, args, context):
    from app.services.agent_tools import execute_tool
    return execute_tool(tool_name, args, db, "u1", context=context)
