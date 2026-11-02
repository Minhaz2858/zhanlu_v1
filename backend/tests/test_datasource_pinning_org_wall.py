"""Phase 7C T1 — pinned-datasource org wall (B1).

The v3 chat path appends a CLIENT-WRITABLE conversation-metadata id
(``conv.metadata_["data_source_id"]``) to the agent's bound set via
``prepare_data_source_runtime(pinned_data_source_id=...)``. Before 7C the
pinned KB was fetched by bare id with NO org check — a conversation owner
could pin any org's datasource into their agent's scope. The pin may only
widen scope WITHIN the agent's own org, and only when an authoritative org
exists to verify against (fail-closed otherwise).
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.agent_app import AgentApp
from app.models.knowledge_base import KnowledgeBase
from app.services import workspace_settings_service
from app.services.data_source_runtime import prepare_data_source_runtime

ORG = "org-acme"
ORG2 = "org-other"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        AgentApp.__table__, KnowledgeBase.__table__])
    s = sessionmaker(bind=eng)()
    workspace_settings_service.clear_cache()
    yield s
    s.close()
    eng.dispose()
    workspace_settings_service.clear_cache()


def _kb(name, org):
    return KnowledgeBase(
        id=str(uuid.uuid4()), name=name, org_id=org,
        type="business_db", source_kind="database", db_type="mysql",
        host="localhost", port=3306, database_name=f"db_{name}",
        username="u", password="p", status="active", is_deleted=False,
        created_date=datetime.utcnow(), updated_date=datetime.utcnow())


def _agent(org):
    return AgentApp(
        id=str(uuid.uuid4()), name="analyst", org_id=org,
        project="default-app", description="", prompt_identity="",
        prompt_boundary="", prompt_reasoning="", prompt_tools="",
        prompt_output="", capabilities=[], skills=[], knowledge_bases=[],
        topology="standalone", sub_agents=[], data_read=True,
        data_write=False, status="active", is_deleted=False,
        created_date=datetime.utcnow(), updated_date=datetime.utcnow())


def _bound(db, agent, pinned_id):
    _t, _p, ctx = prepare_data_source_runtime(
        db=db, agent_app=agent, base_tools=[], base_system_prompt="base",
        pinned_data_source_id=pinned_id)
    return ctx.get("bound_kb_ids") or []


def test_same_org_pinned_datasource_is_allowed(db):
    agent = _agent(ORG)
    own = _kb("own-db", ORG)
    pinned = _kb("pinned-db", ORG)
    agent.knowledge_bases = [own.id]
    db.add_all([agent, own, pinned])
    db.commit()
    bound = _bound(db, agent, pinned.id)
    assert pinned.id in bound and own.id in bound


def test_cross_org_pinned_datasource_is_refused(db):
    agent = _agent(ORG)
    own = _kb("own-db", ORG)
    foreign = _kb("foreign-db", ORG2)
    agent.knowledge_bases = [own.id]
    db.add_all([agent, own, foreign])
    db.commit()
    bound = _bound(db, agent, foreign.id)
    assert foreign.id not in bound  # org wall holds — no cross-tenant pin
    assert own.id in bound


def test_pinned_without_agent_org_is_refused(db):
    # no verifiable org (agent_app=None) → fail closed
    kb = _kb("some-db", ORG)
    db.add(kb)
    db.commit()
    bound = _bound(db, None, kb.id)
    assert kb.id not in bound


def test_deleted_pinned_datasource_is_refused(db):
    agent = _agent(ORG)
    gone = _kb("gone-db", ORG)
    gone.is_deleted = True
    db.add_all([agent, gone])
    db.commit()
    assert gone.id not in _bound(db, agent, gone.id)
