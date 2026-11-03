"""Phase 8 T8-2 — agent binding service + compatibility projection.

The binding service is the ONLY writer of AgentDataBinding rows. Every
write rebuilds the ``AgentApp.knowledge_bases`` JSON projection in the
same transaction so the legacy runtime path keeps working unchanged.
Cross-org access fails closed (PermissionError); missing resources fail
closed (LookupError). Zero bindings → zero bound sources (no fallback).
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.agent_app import AgentApp
from app.models.agent_data_binding import AgentDataBinding
from app.models.knowledge_base import KnowledgeBase
from app.services.agent_binding_service import (
    get_bound_data_source_ids_via_bindings,
    list_bindings, rebuild_projection, revoke_binding, set_binding_enabled,
    upsert_binding,
)

ORG = "org-acme"
OTHER = "org-other"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[
        AgentApp.__table__, AgentDataBinding.__table__,
        KnowledgeBase.__table__])
    s = sessionmaker(bind=eng)()
    s.add(AgentApp(id="app-1", name="Analyst", org_id=ORG))
    s.add(KnowledgeBase(id="ds-1", name="Sales DW", org_id=ORG,
                        source_kind="database"))
    s.add(KnowledgeBase(id="ds-2", name="Contracts", org_id=ORG,
                        source_kind="document"))
    s.add(KnowledgeBase(id="ds-x", name="Foreign DW", org_id=OTHER,
                        source_kind="database"))
    s.commit()
    yield s
    s.close()
    eng.dispose()


def test_upsert_creates_binding_and_projection(db):
    b = upsert_binding(db, ORG, "app-1", "ds-1", actor_id="u1")
    assert b.id and b.is_enabled and b.access_mode == "read_only"
    proj = db.get(AgentApp, "app-1").knowledge_bases
    assert proj == ["ds-1"]  # projection rebuilt in the same transaction


def test_upsert_is_idempotent_one_live_row(db):
    upsert_binding(db, ORG, "app-1", "ds-1", actor_id="u1")
    upsert_binding(db, ORG, "app-1", "ds-1", actor_id="u1",
                   access_mode="read_write")
    rows = db.query(AgentDataBinding).filter_by(
        agent_app_id="app-1", datasource_id="ds-1",
        is_deleted=False).all()
    assert len(rows) == 1 and rows[0].access_mode == "read_write"


def test_upsert_resurrects_soft_deleted_binding(db):
    b = upsert_binding(db, ORG, "app-1", "ds-1", actor_id="u1")
    revoke_binding(db, ORG, b.id, actor_id="u1")
    upsert_binding(db, ORG, "app-1", "ds-1", actor_id="u1")
    live = db.query(AgentDataBinding).filter_by(
        agent_app_id="app-1", datasource_id="ds-1",
        is_deleted=False).all()
    assert len(live) == 1 and live[0].is_enabled is True
    assert db.get(AgentApp, "app-1").knowledge_bases == ["ds-1"]


def test_cross_org_datasource_rejected(db):
    with pytest.raises(PermissionError):
        upsert_binding(db, ORG, "app-1", "ds-x", actor_id="u1")
    assert db.get(AgentApp, "app-1").knowledge_bases in (None, [])


def test_missing_agent_or_datasource_rejected(db):
    with pytest.raises(LookupError):
        upsert_binding(db, ORG, "app-missing", "ds-1", actor_id="u1")
    with pytest.raises(LookupError):
        upsert_binding(db, ORG, "app-1", "ds-missing", actor_id="u1")


def test_cross_org_agent_rejected(db):
    db.add(AgentApp(id="app-f", name="Foreign", org_id=OTHER))
    db.commit()
    with pytest.raises(PermissionError):
        upsert_binding(db, ORG, "app-f", "ds-1", actor_id="u1")


def test_disable_excludes_from_projection_only(db):
    b = upsert_binding(db, ORG, "app-1", "ds-1", actor_id="u1")
    set_binding_enabled(db, ORG, b.id, False, actor_id="u1")
    # row survives (not deleted) but projection drops it
    assert db.get(AgentDataBinding, b.id).is_enabled is False
    assert db.get(AgentApp, "app-1").knowledge_bases == []


def test_revoke_soft_deletes_and_updates_projection(db):
    b1 = upsert_binding(db, ORG, "app-1", "ds-1", actor_id="u1")
    b2 = upsert_binding(db, ORG, "app-1", "ds-2", actor_id="u1")
    revoke_binding(db, ORG, b1.id, actor_id="u1")
    assert db.get(AgentApp, "app-1").knowledge_bases == ["ds-2"]
    assert db.get(AgentDataBinding, b1.id).is_deleted is True
    assert len(list_bindings(db, ORG, "app-1")) == 1


def test_revocation_is_org_scoped(db):
    db.add(AgentApp(id="app-f", name="Foreign", org_id=OTHER))
    db.commit()
    b = upsert_binding(db, ORG, "app-1", "ds-1", actor_id="u1")
    with pytest.raises((PermissionError, LookupError)):
        revoke_binding(db, OTHER, b.id, actor_id="u1")
    assert db.get(AgentDataBinding, b.id).is_deleted is False


def test_zero_bindings_mean_zero_bound_sources(db):
    assert get_bound_data_source_ids_via_bindings(db, "app-1") == []


def test_bound_sources_reads_enabled_live_bindings(db):
    b1 = upsert_binding(db, ORG, "app-1", "ds-1", actor_id="u1")
    upsert_binding(db, ORG, "app-1", "ds-2", actor_id="u1")
    set_binding_enabled(db, ORG, b1.id, False, actor_id="u1")
    assert get_bound_data_source_ids_via_bindings(db, "app-1") == ["ds-2"]
    revoke_binding(db, ORG,
                   db.query(AgentDataBinding).filter_by(
                       datasource_id="ds-2").one().id,
                   actor_id="u1")
    assert get_bound_data_source_ids_via_bindings(db, "app-1") == []


def test_rebuild_projection_direct_call(db):
    upsert_binding(db, ORG, "app-1", "ds-2", actor_id="u1")
    app = db.get(AgentApp, "app-1")
    app.knowledge_bases = ["stale", "junk"]
    rebuild_projection(db, app)
    assert app.knowledge_bases == ["ds-2"]
