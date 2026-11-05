"""P0 Phase 1 tests (2026-10-31): AgentApp-aware delegation.

Covers roadmap P0-1 (AgentApp-aware prompt + toolset), P0-2 (parent→child
bound_kb_ids inheritance with child-identity permission context), and
P0-5 (delegation invocation linked to the parent conversation).
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

from unittest.mock import patch

import pytest

from app.config import settings
from app.database import Base, engine, SessionLocal
import app.models  # noqa: F401
from app.models.agent_app import AgentApp
from app.services.tool_handlers import delegate_tool as dt

# Ensure the tools referenced by the AgentApp toolset exist in the registry.
try:  # pragma: no cover — registration side effects only
    import app.services.tool_handlers.db_tools  # noqa: F401
    import app.services.tool_handlers.query_database_tool  # noqa: F401
    import app.services.tool_handlers.delegation_tools  # noqa: F401
except ImportError:  # pragma: no cover
    pass

APP_NAME = "Pilot Test Agent"
MARKER = "MARKER-5LAYER-PROMPT-XYZ"


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
def published_app(db):
    app = AgentApp(
        name=APP_NAME,
        description="pilot",
        status="published",
        prompt_identity=f"{MARKER} You are the pilot.",
        tool_config={"enabled_tools": ["query_database"]},
        org_id="default-org",
        app_id="default-app",
    )
    db.add(app)
    db.commit()
    yield app
    db.delete(app)
    db.commit()


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(settings, "DELEGATE_AGENTAPP_AWARE", False)
    monkeypatch.setattr(settings, "DELEGATE_INHERIT_BINDINGS", False)
    monkeypatch.setattr(settings, "DELEGATE_VERIFY_ENABLED", False)
    yield


def _fake_llm_final(content="final answer"):
    async def _fake(messages, tool_schemas, endpoint=None):
        return {"content": content, "tool_calls": []}

    return _fake


def _fake_llm_tool_then_final(tool_name="query_database", tool_args={"kb_id": "k"}):
    calls = {"n": 0}

    async def _fake(messages, tool_schemas, endpoint=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "content": "",
                "tool_calls": [{
                    "id": "tc1",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": "{}"},
                }],
            }
        return {"content": "done", "tool_calls": []}

    return _fake


def _tool_names(tool_schemas):
    names = set()
    for s in tool_schemas:
        fn = s.get("function", {}) if isinstance(s, dict) else {}
        if fn.get("name"):
            names.add(fn["name"])
        elif s.get("name"):
            names.add(s["name"])
    return names


# ── P0-1: AgentApp-aware prompt + toolset ──────────────────────────────

async def test_agentapp_prompt_and_toolset_used(db, published_app, monkeypatch):
    # ── Unified tool-access contract (milestone 2026-11-01) ──
    # Default (non-strict) behaviour: enabled_tools are PREFERENCES
    # (priority head — always injected), the rest of the shared registry
    # stays discoverable, and disabled_tools are a hard deny.
    monkeypatch.setattr(settings, "DELEGATE_AGENTAPP_AWARE", True)
    captured = {}

    async def _fake(messages, tool_schemas, endpoint=None):
        captured["system"] = messages[0]["content"]
        captured["tools"] = list(tool_schemas)
        return {"content": "ok", "tool_calls": []}

    monkeypatch.setattr(dt, "call_llm_with_reliability", _fake)
    result = await dt._run_sub_agent_inner(
        "do data things", APP_NAME, db, "u1", 3, None,
        context={}, agent_app=published_app,
    )
    assert result["success"] is True
    assert MARKER in captured["system"], "child must run the AgentApp's 5-layer prompt"
    names = _tool_names(captured["tools"])
    # Priority head (operator preference) is always present…
    assert "query_database" in names, "configured enabled_tools must be injected"
    # …evidence tools are always on…
    assert {"provide_evidence", "request_evidence"} <= names
    # …and nothing outside the registry leaked in.
    from app.services.tool_registry import registry as _reg
    assert names <= set(_reg.list_available()), "injected tools must come from the shared registry"
    assert result["_agent_app_id"] == published_app.id


async def test_agentapp_strict_tools_exact_allowlist(db, published_app, monkeypatch):
    """Operator strict mode: strict_tools=true turns enabled_tools into an
    EXACT allowlist — no discovery beyond the configured tools (plus the
    always-on evidence tools)."""
    monkeypatch.setattr(settings, "DELEGATE_AGENTAPP_AWARE", True)
    published_app.tool_config = {
        "enabled_tools": ["query_database"],
        "strict_tools": True,
    }
    captured = {}

    async def _fake(messages, tool_schemas, endpoint=None):
        captured["tools"] = list(tool_schemas)
        return {"content": "ok", "tool_calls": []}

    monkeypatch.setattr(dt, "call_llm_with_reliability", _fake)
    result = await dt._run_sub_agent_inner(
        "do data things", APP_NAME, db, "u1", 3, None,
        context={}, agent_app=published_app,
    )
    assert result["success"] is True
    assert _tool_names(captured["tools"]) == {
        "query_database", "provide_evidence", "request_evidence",
    }, "strict_tools must restore the exact operator allowlist"


async def test_agentapp_disabled_tools_hard_deny(db, published_app, monkeypatch):
    """disabled_tools is a hard operator deny — it wins even when the same
    tool also appears in enabled_tools, and even in strict mode."""
    monkeypatch.setattr(settings, "DELEGATE_AGENTAPP_AWARE", True)
    published_app.tool_config = {
        "enabled_tools": ["query_database", "provide_evidence"],
        "disabled_tools": ["provide_evidence"],
        "strict_tools": True,
    }
    captured = {}

    async def _fake(messages, tool_schemas, endpoint=None):
        captured["tools"] = list(tool_schemas)
        return {"content": "ok", "tool_calls": []}

    monkeypatch.setattr(dt, "call_llm_with_reliability", _fake)
    await dt._run_sub_agent_inner(
        "do data things", APP_NAME, db, "u1", 3, None,
        context={}, agent_app=published_app,
    )
    names = _tool_names(captured["tools"])
    assert "query_database" in names
    assert "provide_evidence" not in names, "disabled_tools must hard-deny even always-on tools"


def test_bound_child_toolset_bounded_and_protected(monkeypatch):
    """Unit: dynamic discovery bounding — protected head always kept, the
    remainder cut to budget, search_tools escape hatch appended."""
    monkeypatch.setattr(settings, "TOOL_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "TOOL_SEARCH_TOP_K", 8)

    def _schema(n):
        return {"type": "function", "function": {
            "name": n, "description": f"tool {n} data query report",
            "parameters": {"type": "object", "properties": {}},
        }}

    names = [f"tool_{i}" for i in range(40)]
    schemas = [_schema(n) for n in names]
    schemas.append(_schema("query_database"))
    schemas.append(_schema("provide_evidence"))
    out = dt._bound_child_toolset(
        "analyze last month sales performance",
        schemas,
        protected={"query_database", "provide_evidence"},
    )
    out_names = _tool_names(out)
    assert len(out) <= 8 + 1  # budget + appended search_tools
    assert "query_database" in out_names, "protected head must survive bounding"
    assert "provide_evidence" in out_names
    assert "search_tools" in out_names, "escape hatch must be appended"
    assert len(out_names) < len(schemas) + 1


def test_bound_child_toolset_noop_small_catalog(monkeypatch):
    monkeypatch.setattr(settings, "TOOL_SEARCH_ENABLED", True)
    schemas = [{"type": "function", "function": {"name": f"t{i}", "description": "x", "parameters": {}}}
               for i in range(5)]
    out = dt._bound_child_toolset("q", schemas, protected=set())
    assert out == schemas, "small catalog must pass through untouched"


def test_resolve_agent_app_published_only(db, published_app):
    assert dt._resolve_agent_app(db, APP_NAME).id == published_app.id
    assert dt._resolve_agent_app(db, "no-such-agent") is None


async def test_builtin_agent_unaffected(db, monkeypatch):
    monkeypatch.setattr(settings, "DELEGATE_AGENTAPP_AWARE", True)
    from app.services.agent_prompts import _get_all_crud_schemas

    captured = {}

    async def _fake(messages, tool_schemas, endpoint=None):
        captured["system"] = messages[0]["content"]
        captured["tools"] = list(tool_schemas)
        return {"content": "ok", "tool_calls": []}

    monkeypatch.setattr(dt, "call_llm_with_reliability", _fake)
    result = await dt._run_sub_agent_inner(
        "help", "general_assistant", db, "u1", 3, None, context={},
    )
    assert result["success"] is True
    names = _tool_names(captured["tools"])
    crud = set(_get_all_crud_schemas().keys())
    assert names & crud, "builtin child keeps the default toolset incl. CRUD schemas"
    assert len(names) > 5
    assert MARKER not in captured["system"]


async def test_unpublished_app_falls_back(db, monkeypatch):
    monkeypatch.setattr(settings, "DELEGATE_AGENTAPP_AWARE", True)
    app = AgentApp(
        name="Unpublished Pilot",
        status="active",
        prompt_identity=f"{MARKER} active only",
        tool_config={"enabled_tools": ["query_database"]},
        org_id="default-org",
        app_id="default-app",
    )
    db.add(app)
    db.commit()
    try:
        captured = {}

        async def _fake(messages, tool_schemas, endpoint=None):
            captured["system"] = messages[0]["content"]
            captured["tools"] = list(tool_schemas)
            return {"content": "ok", "tool_calls": []}

        monkeypatch.setattr(dt, "call_llm_with_reliability", _fake)
        await dt._run_sub_agent_inner(
            "help", "Unpublished Pilot", db, "u1", 3, None, context={},
        )
        assert MARKER not in captured["system"], "active (unpublished) apps must not drive delegation"
    finally:
        db.delete(app)
        db.commit()


async def test_flag_off_unchanged(db, published_app, monkeypatch):
    monkeypatch.setattr(settings, "DELEGATE_AGENTAPP_AWARE", False)
    captured = {}

    async def _fake(messages, tool_schemas, endpoint=None):
        captured["system"] = messages[0]["content"]
        captured["tools"] = list(tool_schemas)
        return {"content": "ok", "tool_calls": []}

    monkeypatch.setattr(dt, "call_llm_with_reliability", _fake)
    from app.services.agent_prompts import _get_all_crud_schemas

    await dt._run_sub_agent_inner("help", APP_NAME, db, "u1", 3, None, context={})
    assert MARKER not in captured["system"], "flags off = legacy generic-prompt path"
    crud = set(_get_all_crud_schemas().keys())
    assert _tool_names(captured["tools"]) & crud, "flags off = legacy default toolset"


# ── P0-2: binding inheritance + child identity ─────────────────────────

async def test_binding_inheritance_and_child_identity(db, published_app, monkeypatch):
    monkeypatch.setattr(settings, "DELEGATE_AGENTAPP_AWARE", True)
    monkeypatch.setattr(settings, "DELEGATE_INHERIT_BINDINGS", True)
    captured_ctx = {}

    async def _fake_execute(tool_name, arguments, session, user_id, context=None):
        captured_ctx.update(context or {})
        return {"success": True, "data": []}

    async def _fake_llm(messages, tool_schemas, endpoint=None):
        if len(messages) == 2:  # initial system+user → make one tool call
            return {
                "content": "",
                "tool_calls": [{
                    "id": "tc1",
                    "type": "function",
                    "function": {"name": "query_database", "arguments": "{}"},
                }],
            }
        return {"content": "done", "tool_calls": []}

    monkeypatch.setattr("app.services.agent_tools.execute_tool", _fake_execute)
    monkeypatch.setattr(dt, "call_llm_with_reliability", _fake_llm)

    parent_ctx = {
        "conversation_id": "conv-parent-1",
        "bound_kb_ids": ["kb-aaa", "kb-bbb"],
        "agent_name": "general_assistant",  # parent is a meta-agent — must NOT leak
        "agent_app_id": "parent-app-id",
    }
    result = await dt._run_sub_agent_inner(
        "check the data", APP_NAME, db, "u1", 3, None,
        context=parent_ctx, agent_app=published_app,
    )
    assert result["success"] is True
    assert captured_ctx.get("conversation_id") == "conv-parent-1"
    assert captured_ctx.get("bound_kb_ids") == ["kb-aaa", "kb-bbb"]
    assert captured_ctx.get("agent_name") == APP_NAME, (
        "child must be permission-checked as ITSELF, never the parent"
    )
    assert captured_ctx.get("agent_app_id") == published_app.id
    assert captured_ctx.get("agent_name") != "general_assistant"


async def test_no_binding_leak_without_flag(db, published_app, monkeypatch):
    monkeypatch.setattr(settings, "DELEGATE_AGENTAPP_AWARE", True)
    monkeypatch.setattr(settings, "DELEGATE_INHERIT_BINDINGS", False)
    captured_ctx = {}

    async def _fake_execute(tool_name, arguments, session, user_id, context=None):
        captured_ctx.update(context or {})
        return {"success": True, "data": []}

    async def _fake_llm(messages, tool_schemas, endpoint=None):
        if len(messages) == 2:
            return {
                "content": "",
                "tool_calls": [{
                    "id": "tc1",
                    "type": "function",
                    "function": {"name": "query_database", "arguments": "{}"},
                }],
            }
        return {"content": "done", "tool_calls": []}

    monkeypatch.setattr("app.services.agent_tools.execute_tool", _fake_execute)
    monkeypatch.setattr(dt, "call_llm_with_reliability", _fake_llm)

    await dt._run_sub_agent_inner(
        "x", APP_NAME, db, "u1", 3, None,
        context={"conversation_id": "c1", "bound_kb_ids": ["kb-1"]},
    )
    assert "bound_kb_ids" not in captured_ctx, "flag off = no inheritance"
    assert captured_ctx.get("agent_name") == APP_NAME


# ── P0-5: invocation linking ────────────────────────────────────────────

async def test_invocation_links_conversation(db, published_app, monkeypatch):
    monkeypatch.setattr(settings, "DELEGATE_AGENTAPP_AWARE", True)
    captured = {}

    def _fake_record_invocation(db_session, **kwargs):
        captured.update(kwargs)
        return "inv-1"

    monkeypatch.setattr(
        "app.services.agent_invocations.record_invocation", _fake_record_invocation
    )
    monkeypatch.setattr(dt, "call_llm_with_reliability", _fake_llm_final("answer"))

    result = await dt._run_sub_agent(
        "analyze", APP_NAME, db, "u1", 3, None,
        context={"conversation_id": "conv-9"},
    )
    assert result["success"] is True
    assert captured.get("conversation_id") == "conv-9"
    assert captured.get("agent_app_id") == published_app.id
    assert captured.get("invocation_type") == "sub_agent"
    assert captured.get("output_json", {}).get("agent_name") == APP_NAME
    assert "_agent_app_id" not in result, "internal key must be stripped before parent sees it"
