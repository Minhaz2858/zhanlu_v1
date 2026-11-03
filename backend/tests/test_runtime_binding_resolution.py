"""Phase 8 T8-2/T8-5 — runtime resolves the binding-authoritative bound set.

When an agent has AgentDataBinding rows they are authoritative; the legacy
``AgentApp.knowledge_bases`` JSON is only a fallback for agents without
bindings. Revocation/disable takes effect on the NEXT resolution (the
runtime resolves per turn — no cache).
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.agent_app import AgentApp
from app.models.agent_data_binding import AgentDataBinding
from app.models.knowledge_base import KnowledgeBase
from app.models.workspace_settings import WorkspaceSetting
from app.services.agent_binding_service import (
    revoke_binding, set_binding_enabled, upsert_binding)
from app.services.data_source_runtime.data_source_runtime import (
    _prepare_data_source_runtime_impl)

ORG = "org-acme"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[
        AgentApp.__table__, KnowledgeBase.__table__,
        WorkspaceSetting.__table__, AgentDataBinding.__table__])
    s = sessionmaker(bind=eng)()
    s.add(AgentApp(id="app-r", name="Analyst", org_id=ORG))
    s.add(KnowledgeBase(id="ds-1", name="Sales DW", org_id=ORG,
                        source_kind="database"))
    s.commit()
    yield s
    s.close()
    eng.dispose()


def _resolve(db, agent):
    _, _, ctx = _prepare_data_source_runtime_impl(
        db, agent, [], "system prompt")
    return list(ctx.get("bound_kb_ids") or [])


def test_bound_set_reflects_bindings(db):
    agent = db.get(AgentApp, "app-r")
    upsert_binding(db, ORG, "app-r", "ds-1", actor_id="u1")
    assert _resolve(db, agent) == ["ds-1"]


def test_revocation_effective_next_turn(db):
    agent = db.get(AgentApp, "app-r")
    b = upsert_binding(db, ORG, "app-r", "ds-1", actor_id="u1")
    assert _resolve(db, agent) == ["ds-1"]
    revoke_binding(db, ORG, b.id, actor_id="u1")
    assert _resolve(db, agent) == []  # NEXT turn — no cache


def test_disable_effective_next_turn(db):
    agent = db.get(AgentApp, "app-r")
    b = upsert_binding(db, ORG, "app-r", "ds-1", actor_id="u1")
    set_binding_enabled(db, ORG, b.id, False, actor_id="u1")
    assert _resolve(db, agent) == []


def test_agent_without_bindings_falls_back_to_json_projection(db):
    agent = db.get(AgentApp, "app-r")
    agent.knowledge_bases = ["ds-1"]  # legacy-only agent
    db.commit()
    assert _resolve(db, agent) == ["ds-1"]
