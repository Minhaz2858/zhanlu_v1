"""Phase 8.2 Stages 3+4/6 — the tool-execution authorization boundary.

``resolve_execution_context`` is the single choke point where an external
MCP caller's identity, claims, and datasource scope are resolved. This
matrix proves the security invariants: no cross-org access, no privilege
escalation through forged context, no datasource access without an
enabled same-org binding, no default/first-datasource or query-all.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.agent_app import AgentApp
from app.models.agent_data_binding import AgentDataBinding
from app.models.knowledge_base import KnowledgeBase
from app.models.organization import Organization
from app.models.user import User
from app.services.mcp_server.mcp_auth import resolve_execution_context

PRINCIPAL = {"user_id": "u1", "org_id": "org-ok", "role": "user"}


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[
        Organization.__table__, AgentApp.__table__,
        AgentDataBinding.__table__, KnowledgeBase.__table__,
        User.__table__])
    s = sessionmaker(bind=eng)()
    s.add(Organization(id="org-ok", name="OK", slug="ok"))
    s.add(Organization(id="org-other", name="Other", slug="other"))
    s.add(AgentApp(id="app-1", name="Analyst", org_id="org-ok"))
    s.add(AgentApp(id="app-x", name="Rival", org_id="org-other"))
    s.add(KnowledgeBase(id="ds-1", name="DW", org_id="org-ok",
                        app_id="default-app", source_kind="database"))
    s.add(KnowledgeBase(id="ds-2", name="Docs", org_id="org-ok",
                        app_id="default-app", source_kind="file"))
    s.add(KnowledgeBase(id="ds-x", name="Foreign", org_id="org-other",
                        app_id="default-app", source_kind="database"))
    s.commit()
    yield s
    s.close()
    eng.dispose()


def _bind(db, agent, ds, enabled=True):
    db.add(AgentDataBinding(agent_app_id=agent, datasource_id=ds,
                            is_enabled=enabled))
    db.commit()


def test_anonymous_no_context_stays_none(db):
    ctx, _ = resolve_execution_context(db, {}, None)
    assert ctx is None  # fail-honest no-context fallback


def test_anonymous_claims_carry_no_identity_authority(db):
    ctx, _ = resolve_execution_context(
        db, {"_zhanlu_context": {"org_id": "org-other",
                                 "agent_app_id": "app-x"}}, None)
    # existence-verified only — never trusted as identity
    assert ctx is None or ctx.get("org_id") != "org-other" or \
        ctx.get("verified") is not True


def test_authenticated_identity_overrides_claims(db):
    ctx, _ = resolve_execution_context(
        db, {"_zhanlu_context": {"org_id": "org-other",
                                 "user_id": "attacker"}},
        PRINCIPAL)
    assert ctx["org_id"] == "org-ok"      # authoritative
    assert ctx["user_id"] == "u1"
    assert ctx["verified"] is True


def test_mismatching_claims_drop_agent_selection(db):
    # forged agent claim alongside mismatched org — the whole context is
    # dropped, so NO agent scoping (and thus no bound sources) results
    ctx, _ = resolve_execution_context(
        db, {"_zhanlu_context": {"org_id": "org-other",
                                 "agent_app_id": "app-1"}}, PRINCIPAL)
    assert ctx is None or ctx.get("agent_app_id") != "app-1"


def test_cross_org_agent_never_enters_context(db):
    ctx, _ = resolve_execution_context(
        db, {"_zhanlu_context": {"agent_app_id": "app-x"}}, PRINCIPAL)
    assert ctx.get("agent_app_id") != "app-x"


def test_enabled_binding_drives_datasource_scope(db):
    _bind(db, "app-1", "ds-1")
    ctx, _ = resolve_execution_context(
        db, {"_zhanlu_context": {"agent_app_id": "app-1"}}, PRINCIPAL)
    assert ctx["bound_kb_ids"] == ["ds-1"]  # ONLY the bound datasource


def test_disabled_binding_yields_no_scope(db):
    _bind(db, "app-1", "ds-1", enabled=False)
    ctx, _ = resolve_execution_context(
        db, {"_zhanlu_context": {"agent_app_id": "app-1"}}, PRINCIPAL)
    assert ctx["bound_kb_ids"] == []


def test_no_binding_no_scope_no_default_no_query_all(db):
    ctx, _ = resolve_execution_context(
        db, {"_zhanlu_context": {"agent_app_id": "app-1"}}, PRINCIPAL)
    assert ctx["bound_kb_ids"] == []


def test_cross_org_datasource_never_in_scope(db):
    _bind(db, "app-1", "ds-1")
    # ds-x belongs to org-other and is NOT bound to app-1 — it can never
    # appear, whatever the caller claims
    ctx, _ = resolve_execution_context(
        db, {"_zhanlu_context": {"agent_app_id": "app-1",
                                 "bound_kb_ids": ["ds-x"]}}, PRINCIPAL)
    assert "ds-x" not in ctx["bound_kb_ids"]
