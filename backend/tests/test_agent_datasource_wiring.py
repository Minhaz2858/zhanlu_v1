"""Tests for the agent-datasource wiring fix.

Three contracts are pinned by these tests:

1. ``prepare_data_source_runtime`` honors a workspace opt-in flag — when the
   flag is on, every connected database KB in the same workspace is unioned
   with the agent's bound KB list (so any agent can read from any connected
   datasource), and the returned ``bound_kb_ids`` reflects that union.

2. ``prepare_data_source_runtime`` is opt-in safe by default — when the flag
   is off (the default), the bound list is exactly the agent's own
   ``knowledge_bases``. DATA-CORE-3 invariant is preserved.

3. The DB tool handlers (``_require_kb_id``) still reject KB ids that the
   agent is not bound to, even when the workspace opt-in flag is on for
   *other* KBs. The enforcement lives in the tool, not in the prompt.

Tests use SQLite in-memory + real models (no mocks for the DB) and exercise
``prepare_data_source_runtime`` + ``_require_kb_id`` directly. They run
without network, LLM, or live database connections.
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models.agent_app import AgentApp
from app.models.knowledge_base import KnowledgeBase
from app.models.workspace_settings import WorkspaceSetting
from app.services import workspace_settings_service
from app.services.data_source_runtime import prepare_data_source_runtime
from app.services.tool_handlers import db_tools
from app.services.tool_handlers.db_tools import _require_kb_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session():
    """Provide a clean in-memory SQLite session with the schema migrated.

    We patch ``settings.DATABASE_URL`` to in-memory only for the duration of
    the test. We register the real ``WorkspaceSetting`` model so its table
    exists, but we use the real ``AgentApp`` / ``KnowledgeBase`` models.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    # Create schema for the three models we use. We do NOT use the global
    # Base.metadata.create_all because the real DB has dozens of unrelated
    # tables — keeping the test schema tiny makes failures legible.
    Base.metadata.create_all(
        engine,
        tables=[
            AgentApp.__table__,
            KnowledgeBase.__table__,
            WorkspaceSetting.__table__,
        ],
    )
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    # Workspace settings are in-process memoized for 5s. Clear the
    # cache before each test so a previous test's "true" value can't
    # leak into the next test.
    workspace_settings_service.clear_cache()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        workspace_settings_service.clear_cache()


def _make_kb(name: str, project: str = "default-app") -> KnowledgeBase:
    """Insert a database KB row and return it."""
    kb = KnowledgeBase(
        id=str(uuid.uuid4()),
        name=name,
        project=project,
        type="business_db",
        source_kind="database",
        db_type="mysql",
        host="localhost",
        port=3306,
        database_name="db_" + name,
        username="u",
        password="p",
        status="active",
        is_deleted=False,
        created_date=datetime.utcnow(),
        updated_date=datetime.utcnow(),
    )
    return kb


def _make_agent(name: str, kb_ids: list[str]) -> AgentApp:
    """Insert an AgentApp row with the given knowledge_bases binding."""
    return AgentApp(
        id=str(uuid.uuid4()),
        name=name,
        project="default-app",
        description="",
        prompt_identity="",
        prompt_boundary="",
        prompt_reasoning="",
        prompt_tools="",
        prompt_output="",
        capabilities=["market_analysis"],
        skills=[],
        knowledge_bases=kb_ids,
        topology="standalone",
        sub_agents=[],
        data_read=True,
        data_write=False,
        status="active",
        is_deleted=False,
        created_date=datetime.utcnow(),
        updated_date=datetime.utcnow(),
    )


# ---------------------------------------------------------------------------
# Contract 1: opt-in flag unions all connected DB KBs
# ---------------------------------------------------------------------------


def test_opt_in_flag_unions_all_connected_db_kbs(db_session):
    """When workspace setting 'auto_bind_all_datasources' is on, the bound
    KB list returned by ``prepare_data_source_runtime`` includes every
    connected database KB in the same workspace — not just the ones the
    user explicitly added to the agent.
    """
    bound_kb = _make_kb("bound")
    other_kb = _make_kb("other")
    db_session.add_all([bound_kb, other_kb])
    db_session.commit()

    agent = _make_agent("agent", [bound_kb.id])
    db_session.add(agent)
    db_session.commit()

    # The opt-in flag is on for this workspace.
    db_session.add(WorkspaceSetting(
        id=str(uuid.uuid4()),
        key="auto_bind_all_datasources",
        value="true",
        org_id="default-org",
        app_id="default-app",
    ))
    db_session.commit()

    _tools, _prompt, ctx_extras = prepare_data_source_runtime(
        db=db_session,
        agent_app=agent,
        base_tools=[],
        base_system_prompt="base",
    )
    bound_ids = ctx_extras.get("bound_kb_ids") or []
    # Both the agent's explicit KB AND the other connected DB KB must be in
    # the bound list when the flag is on.
    assert bound_kb.id in bound_ids
    assert other_kb.id in bound_ids


# ---------------------------------------------------------------------------
# Contract 2: opt-in is OFF by default (DATA-CORE-3)
# ---------------------------------------------------------------------------


def test_opt_in_flag_default_off_only_uses_agent_bound_kbs(db_session):
    """When the flag is NOT set, the bound list is exactly the agent's
    own ``knowledge_bases`` — DATA-CORE-3 invariant.
    """
    bound_kb = _make_kb("bound")
    other_kb = _make_kb("other")
    db_session.add_all([bound_kb, other_kb])
    db_session.commit()

    agent = _make_agent("agent", [bound_kb.id])
    db_session.add(agent)
    db_session.commit()

    _tools, _prompt, ctx_extras = prepare_data_source_runtime(
        db=db_session,
        agent_app=agent,
        base_tools=[],
        base_system_prompt="base",
    )
    bound_ids = ctx_extras.get("bound_kb_ids") or []
    assert bound_ids == [bound_kb.id]
    assert other_kb.id not in bound_ids


def test_opt_in_flag_explicitly_off_same_as_default(db_session):
    """An explicit ``value='false'`` is the same as the default — only
    the agent's own KBs are bound.
    """
    bound_kb = _make_kb("bound")
    other_kb = _make_kb("other")
    db_session.add_all([bound_kb, other_kb])
    db_session.commit()

    agent = _make_agent("agent", [bound_kb.id])
    db_session.add(agent)
    db_session.commit()

    db_session.add(WorkspaceSetting(
        id=str(uuid.uuid4()),
        key="auto_bind_all_datasources",
        value="false",
        org_id="default-org",
        app_id="default-app",
    ))
    db_session.commit()

    _tools, _prompt, ctx_extras = prepare_data_source_runtime(
        db=db_session,
        agent_app=agent,
        base_tools=[],
        base_system_prompt="base",
    )
    bound_ids = ctx_extras.get("bound_kb_ids") or []
    assert bound_ids == [bound_kb.id]
    assert other_kb.id not in bound_ids


# ---------------------------------------------------------------------------
# Contract 3: tool handlers still enforce per-agent bound_kb_ids
# ---------------------------------------------------------------------------


def test_require_kb_id_rejects_unbound_kb_even_with_opt_in(db_session):
    """The DB tool handlers enforce the per-agent ``bound_kb_ids`` set
    received in the call context. If a tool call arrives with a KB id
    that isn't in the bound list, the tool returns a clear error — even
    when the workspace opt-in flag is on, because the runtime still
    scopes the per-call ``bound_kb_ids`` correctly.
    """
    bound_kb = _make_kb("bound")
    other_kb = _make_kb("other")
    db_session.add_all([bound_kb, other_kb])
    db_session.commit()

    # The agent is bound to `other_kb` only. `bound_kb` is NOT in the list.
    # The tool call arrives with data_source_id=bound_kb.id but the context
    # only contains other_kb.id. The tool must reject.
    ctx = {"bound_kb_ids": [other_kb.id]}

    kb_id, err = _require_kb_id(
        args={"data_source_id": bound_kb.id},
        context=ctx,
    )
    assert kb_id is None
    assert err is not None
    assert err["success"] is False
    # The error must be specific — it must name the offending KB id and
    # the bound set so debugging is obvious in the chat UI.
    assert bound_kb.id in err["error"]
    assert other_kb.id in err["error"]


def test_require_kb_id_accepts_bound_kb(db_session):
    """The inverse: a KB id that IS in the bound list is accepted."""
    bound_kb = _make_kb("bound")
    db_session.add(bound_kb)
    db_session.commit()

    ctx = {"bound_kb_ids": [bound_kb.id]}
    kb_id, err = _require_kb_id(
        args={"data_source_id": bound_kb.id},
        context=ctx,
    )
    assert kb_id == bound_kb.id
    assert err is None


# ---------------------------------------------------------------------------
# Contract 4: tools list contains ask_data_agent when ANY KB is bound
# ---------------------------------------------------------------------------


def test_ask_data_agent_in_tool_list_when_kbs_bound(db_session):
    """When the agent has at least one bound KB (whether explicit or
    auto-bound), the returned tool list must contain the
    ``ask_data_agent`` schema — that's the only way the LLM can reach
    the database.
    """
    bound_kb = _make_kb("bound")
    db_session.add(bound_kb)
    db_session.commit()

    agent = _make_agent("agent", [bound_kb.id])
    db_session.add(agent)
    db_session.commit()

    tools, _prompt, _ctx = prepare_data_source_runtime(
        db=db_session,
        agent_app=agent,
        base_tools=[],
        base_system_prompt="base",
    )
    tool_names = {t.get("function", {}).get("name") for t in tools}
    assert "ask_data_agent" in tool_names


def test_ask_data_agent_NOT_in_tool_list_when_no_kbs_bound(db_session):
    """The inverse: when the agent has no bound KBs, ``ask_data_agent``
    is NOT injected.
    """
    agent = _make_agent("agent", [])
    db_session.add(agent)
    db_session.commit()

    tools, _prompt, _ctx = prepare_data_source_runtime(
        db=db_session,
        agent_app=agent,
        base_tools=[],
        base_system_prompt="base",
    )
    tool_names = {t.get("function", {}).get("name") for t in tools}
    assert "ask_data_agent" not in tool_names


# ---------------------------------------------------------------------------
# Contract 5: file-kind KBs flow through the runtime the same way database KBs do
# ---------------------------------------------------------------------------
#
# When the user reports "agent can give answers from files", the wiring
# pipeline (prompt + tool list + bound_kb_ids) must include uploaded file
# KBs with the same fidelity as database KBs. These tests pin that contract
# so a future refactor cannot silently drop file KBs from the agent's
# runtime surface.


def _make_file_kb(
    name: str,
    project: str = "default-app",
    file_type: str = "pdf",
    indexing_status: str = "indexed",
    chunk_count: int = 4,
) -> KnowledgeBase:
    """Insert a file-kind KB row (uploaded document) and return it."""
    return KnowledgeBase(
        id=str(uuid.uuid4()),
        name=name,
        project=project,
        type="file",
        source_kind="file",
        file_type=file_type,
        file_url="/api/uploads/" + name + "." + file_type,
        indexing_status=indexing_status,
        chunk_count=chunk_count,
        status="active",
        is_deleted=False,
        created_date=datetime.utcnow(),
        updated_date=datetime.utcnow(),
    )


def test_file_kb_appears_in_prompt_section(db_session):
    """When the agent binds a file-kind KB, the system prompt must list
    it under the 'Document sources' heading so the LLM knows it can
    answer questions by reading the uploaded document.
    """
    file_kb = _make_file_kb("quarterly-report")
    db_session.add(file_kb)
    db_session.commit()

    agent = _make_agent("agent", [file_kb.id])
    db_session.add(agent)
    db_session.commit()

    _tools, prompt, ctx = prepare_data_source_runtime(
        db=db_session,
        agent_app=agent,
        base_tools=[],
        base_system_prompt="base",
    )
    # Bound KB id set must contain the file KB.
    bound_ids = ctx.get("bound_kb_ids") or []
    assert file_kb.id in bound_ids
    # Prompt must describe the file KB so the LLM can call ask_data_agent
    # against it. Both the heading AND the KB name must appear, and the
    # file metadata (file_type, chunk_count) must surface for the LLM.
    assert "Bound Data Sources" in prompt
    assert "Document sources" in prompt
    assert "quarterly-report" in prompt
    assert "file_type=`pdf`" in prompt
    assert "chunks=4" in prompt


def test_file_kb_triggers_ask_data_agent_tool(db_session):
    """A bound file KB alone is enough to inject ``ask_data_agent`` into
    the tool list — the LLM uses that tool to delegate vector retrieval
    to the Data Agent. Without it the LLM cannot answer questions about
    uploaded documents.
    """
    file_kb = _make_file_kb("handbook")
    db_session.add(file_kb)
    db_session.commit()

    agent = _make_agent("agent", [file_kb.id])
    db_session.add(agent)
    db_session.commit()

    tools, _prompt, _ctx = prepare_data_source_runtime(
        db=db_session,
        agent_app=agent,
        base_tools=[],
        base_system_prompt="base",
    )
    tool_names = {t.get("function", {}).get("name") for t in tools}
    assert "ask_data_agent" in tool_names


def test_mixed_db_and_file_kbs_both_appear_in_prompt(db_session):
    """When the agent binds both a database and a file KB, both must
    surface in the prompt — one under 'Database sources', one under
    'Document sources' — and the bound id set must include both. This
    is the common 'agent can answer from files AND from databases' case.
    """
    db_kb = _make_kb("erp")
    file_kb = _make_file_kb("addresses", file_type="csv")
    db_session.add_all([db_kb, file_kb])
    db_session.commit()

    agent = _make_agent("agent", [db_kb.id, file_kb.id])
    db_session.add(agent)
    db_session.commit()

    tools, prompt, ctx = prepare_data_source_runtime(
        db=db_session,
        agent_app=agent,
        base_tools=[],
        base_system_prompt="base",
    )

    # Both KBs are bound.
    bound_ids = ctx.get("bound_kb_ids") or []
    assert db_kb.id in bound_ids
    assert file_kb.id in bound_ids

    # Both KBs are described in the prompt.
    assert "Database sources" in prompt
    assert "Document sources" in prompt
    assert "erp" in prompt
    assert "addresses" in prompt

    # ask_data_agent is exposed so the LLM can reach either kind.
    tool_names = {t.get("function", {}).get("name") for t in tools}
    assert "ask_data_agent" in tool_names


# ---------------------------------------------------------------------------
# Contract 6: dashboard build toolset injected for data-bound agents
# ---------------------------------------------------------------------------
# 2026-08-27 regression (conv 3e7fa92b, C5_C9): a chat served by
# general_assistant (whose enabled_tools lack create_fullstack_dashboard)
# could explore the schema via injected data tools but could NEVER call the
# build tool — the dashboard guards are all inert when the tool is absent,
# so the turn ended with a "readiness assessment" report instead of the app.
# Fix: prepare_data_source_runtime injects the full dashboard pipeline
# (build + design + grounding) alongside ask_data_agent/fetch_data_batch
# whenever the agent has bound data sources and the fullstack flag is on.


def test_dashboard_tools_injected_when_kbs_bound_and_fullstack_on(db_session, monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", True)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", False)

    bound_kb = _make_kb("bound")
    db_session.add(bound_kb)
    db_session.commit()

    agent = _make_agent("agent", [bound_kb.id])
    db_session.add(agent)
    db_session.commit()

    tools, _prompt, _ctx = prepare_data_source_runtime(
        db=db_session,
        agent_app=agent,
        base_tools=[],
        base_system_prompt="base",
    )
    tool_names = {t.get("function", {}).get("name") for t in tools}

    # Build pipeline
    assert "create_fullstack_dashboard" in tool_names
    assert "update_fullstack_dashboard" in tool_names
    # Design intelligence
    assert "uiux_design_system" in tool_names
    assert "uiux_search" in tool_names
    # Schema grounding (the dashboard guards require describe_schema evidence)
    assert "list_data_sources" in tool_names
    assert "describe_schema" in tool_names
    # Data tools still injected
    assert "ask_data_agent" in tool_names
    assert "fetch_data_batch" in tool_names


def test_dashboard_tools_NOT_injected_when_fullstack_off(db_session, monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False)

    bound_kb = _make_kb("bound")
    db_session.add(bound_kb)
    db_session.commit()

    agent = _make_agent("agent", [bound_kb.id])
    db_session.add(agent)
    db_session.commit()

    tools, _prompt, _ctx = prepare_data_source_runtime(
        db=db_session,
        agent_app=agent,
        base_tools=[],
        base_system_prompt="base",
    )
    tool_names = {t.get("function", {}).get("name") for t in tools}
    assert "create_fullstack_dashboard" not in tool_names


def test_dashboard_tools_NOT_injected_when_no_kbs_bound(db_session, monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", True)

    agent = _make_agent("agent", [])
    db_session.add(agent)
    db_session.commit()

    tools, _prompt, _ctx = prepare_data_source_runtime(
        db=db_session,
        agent_app=agent,
        base_tools=[],
        base_system_prompt="base",
    )
    tool_names = {t.get("function", {}).get("name") for t in tools}
    assert "create_fullstack_dashboard" not in tool_names


def test_dashboard_tools_injection_is_idempotent(db_session, monkeypatch):
    """If the agent already whitelists create_fullstack_dashboard, the
    runtime must not duplicate its schema."""
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", True)

    bound_kb = _make_kb("bound")
    db_session.add(bound_kb)
    db_session.commit()

    from app.services.tool_registry import normalize_tool_schema, registry

    dash_entry = registry.get_entry("create_fullstack_dashboard")
    base_tools = [normalize_tool_schema(dash_entry.schema, fallback_name=dash_entry.name)]

    agent = _make_agent("agent", [bound_kb.id])
    db_session.add(agent)
    db_session.commit()

    tools, _prompt, _ctx = prepare_data_source_runtime(
        db=db_session,
        agent_app=agent,
        base_tools=base_tools,
        base_system_prompt="base",
    )
    names = [t.get("function", {}).get("name") for t in tools]
    assert names.count("create_fullstack_dashboard") == 1
