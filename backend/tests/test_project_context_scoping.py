"""Tests for contextual project scoping in ``prepare_data_source_runtime``.

These pin the user-requested behavior:

1. **No project selected** — the agent reads ONLY its own bound data
   sources. It does NOT auto-inherit KBs from projects it is a member of.
   (The "agent + no project = agent's own data only" rule.)

2. **A project is selected** — the agent reads its own bound data sources
   UNION that project's KBs, AND a "Project Context" block (project
   description + shared project memory) is injected into the system prompt.

3. **A project is selected but the agent has no data sources** — the
   project context is still injected (the agent is project-aware even for
   non-data questions), and ``ask_data_agent`` is NOT injected.

Tests use SQLite in-memory + real models (no mocks) and exercise
``prepare_data_source_runtime`` directly — no network, LLM, or live DB.
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.agent_app import AgentApp
from app.models.knowledge_base import KnowledgeBase
from app.models.project import Project
from app.models.project_memory import ProjectMemory
from app.models.project_agent import ProjectAgent
from app.services import workspace_settings_service
from app.services.data_source_runtime import prepare_data_source_runtime


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session():
    """Clean in-memory SQLite session with the schema the scoping tests need."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(
        engine,
        tables=[
            AgentApp.__table__,
            KnowledgeBase.__table__,
            Project.__table__,
            ProjectMemory.__table__,
            ProjectAgent.__table__,
        ],
    )
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    workspace_settings_service.clear_cache()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        workspace_settings_service.clear_cache()


def _make_kb(name: str, project_id: str | None = None) -> KnowledgeBase:
    return KnowledgeBase(
        id=str(uuid.uuid4()),
        name=name,
        project="legacy",
        project_id=project_id,
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


def _make_agent(name: str, kb_ids: list[str], project_id: str | None = None) -> AgentApp:
    return AgentApp(
        id=str(uuid.uuid4()),
        name=name,
        project="legacy",
        project_id=project_id,
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


def _make_project(name: str, description: str = "") -> Project:
    return Project(
        id=str(uuid.uuid4()),
        name=name,
        description=description or None,
        color="#000000",
        status="active",
        is_deleted=False,
        created_date=datetime.utcnow(),
        updated_date=datetime.utcnow(),
    )


def _make_memory(project_id: str, content: str, entry_type: str = "fact",
                 importance: int = 0) -> ProjectMemory:
    return ProjectMemory(
        id=str(uuid.uuid4()),
        project_id=project_id,
        agent_app_id=None,
        entry_type=entry_type,
        content=content,
        importance=importance,
        is_deleted=False,
        created_date=datetime.utcnow(),
        updated_date=datetime.utcnow(),
    )


# ---------------------------------------------------------------------------
# Contract 1: no project selected → agent's own KBs only (no membership leak)
# ---------------------------------------------------------------------------


def test_no_project_selected_does_not_inherit_member_project_kbs(db_session):
    """When no project is selected, the agent must NOT inherit KBs from
    projects it is a member of — even though a ProjectAgent row exists and
    a KB is scoped to that project. Only the agent's own bound KB applies.
    """
    project = _make_project("Acme")
    db_session.add(project)
    db_session.commit()

    own_kb = _make_kb("own")
    project_kb = _make_kb("project_kb", project_id=project.id)
    db_session.add_all([own_kb, project_kb])
    db_session.commit()

    agent = _make_agent("agent", [own_kb.id], project_id=project.id)
    db_session.add(agent)
    # Also create a many-to-many membership to prove it is ignored when no
    # project is selected for the conversation.
    db_session.add(ProjectAgent(
        id=str(uuid.uuid4()),
        project_id=project.id,
        agent_id=agent.id,
        role="member",
        is_deleted=False,
        created_date=datetime.utcnow(),
        updated_date=datetime.utcnow(),
    ))
    db_session.commit()

    _tools, _prompt, ctx = prepare_data_source_runtime(
        db=db_session,
        agent_app=agent,
        base_tools=[],
        base_system_prompt="base",
        selected_project_id=None,
    )
    bound_ids = ctx.get("bound_kb_ids") or []
    assert bound_ids == [own_kb.id]
    assert project_kb.id not in bound_ids


# ---------------------------------------------------------------------------
# Contract 2: project selected → own KBs ∪ selected project's KBs + context
# ---------------------------------------------------------------------------


def test_project_selected_unions_project_kbs_and_injects_context(db_session):
    """When a project is selected, the bound list is the agent's own KBs
    UNION the selected project's KBs, and the system prompt contains a
    "Project Context" block with the project name, description, and shared
    memory entries.
    """
    project = _make_project("Acme", description="Acme sales analytics")
    db_session.add(project)
    db_session.commit()

    own_kb = _make_kb("own")
    project_kb = _make_kb("project_kb", project_id=project.id)
    other_project = _make_project("Other")
    db_session.add_all([own_kb, project_kb, other_project])
    other_kb = _make_kb("other_kb", project_id=other_project.id)
    db_session.add(other_kb)
    db_session.commit()

    # Shared memory entries for the selected project.
    db_session.add_all([
        _make_memory(project.id, "Q3 target is 12M", entry_type="decision", importance=5),
        _make_memory(project.id, "Top region is APAC", entry_type="data_insight", importance=3),
        # A memory for the *other* project — must NOT appear.
        _make_memory(other_project.id, "Other project secret", entry_type="fact"),
    ])
    db_session.commit()

    agent = _make_agent("agent", [own_kb.id])
    db_session.add(agent)
    db_session.commit()

    tools, prompt, ctx = prepare_data_source_runtime(
        db=db_session,
        agent_app=agent,
        base_tools=[],
        base_system_prompt="base",
        selected_project_id=project.id,
    )
    bound_ids = ctx.get("bound_kb_ids") or []
    # Own KB + selected project's KB. The other project's KB is excluded.
    assert own_kb.id in bound_ids
    assert project_kb.id in bound_ids
    assert other_kb.id not in bound_ids

    # ask_data_agent injected because there are bound DB KBs.
    tool_names = {t.get("function", {}).get("name") for t in tools}
    assert "ask_data_agent" in tool_names

    # Project context block injected with name, description, and memories.
    assert "## Project Context" in prompt
    assert "Acme" in prompt
    assert "Acme sales analytics" in prompt
    assert "Q3 target is 12M" in prompt
    assert "Top region is APAC" in prompt
    # The other project's memory must not leak in.
    assert "Other project secret" not in prompt


# ---------------------------------------------------------------------------
# Contract 3: project selected, no data sources → context only, no DB tool
# ---------------------------------------------------------------------------


def test_project_selected_no_data_sources_injects_context_without_db_tool(db_session):
    """When a project is selected but the agent has no bound data sources
    (and the project has no KBs), the project context is still injected and
    ``ask_data_agent`` is NOT added to the tool list.
    """
    project = _make_project("Acme", description="Empty project")
    db_session.add(project)
    db_session.commit()
    db_session.add(_make_memory(project.id, "A known fact", entry_type="fact"))
    db_session.commit()

    agent = _make_agent("agent", [])
    db_session.add(agent)
    db_session.commit()

    tools, prompt, ctx = prepare_data_source_runtime(
        db=db_session,
        agent_app=agent,
        base_tools=[],
        base_system_prompt="base",
        selected_project_id=project.id,
    )
    # No bound data sources → no ctx_extras bound_kb_ids, no DB tool.
    assert not ctx.get("bound_kb_ids")
    tool_names = {t.get("function", {}).get("name") for t in tools}
    assert "ask_data_agent" not in tool_names
    # Project context still injected.
    assert "## Project Context" in prompt
    assert "Acme" in prompt
    assert "A known fact" in prompt


def test_project_selected_but_deleted_project_yields_no_context(db_session):
    """A soft-deleted project must not produce a context block."""
    project = _make_project("Ghost")
    project.is_deleted = True
    db_session.add(project)
    db_session.commit()

    agent = _make_agent("agent", [])
    db_session.add(agent)
    db_session.commit()

    _tools, prompt, ctx = prepare_data_source_runtime(
        db=db_session,
        agent_app=agent,
        base_tools=[],
        base_system_prompt="base",
        selected_project_id=project.id,
    )
    assert "## Project Context" not in prompt
    assert not ctx.get("bound_kb_ids")
