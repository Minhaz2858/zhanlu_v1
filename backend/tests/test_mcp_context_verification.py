"""Phase 8.1 Stage 2 — MCP context claims are VERIFIED server-side.

``_zhanlu_context`` is caller-supplied, so before it may influence any
authorization decision every identity claim is validated against the
database: the org must exist, the agent (when claimed) must exist, be
live, and belong to that org, and the user (when claimed) must belong to
that org. ANY unverifiable claim drops the WHOLE context to None — the
fail-honest no-context fallback — never a partially-trusted one.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.agent_app import AgentApp
from app.models.organization import Organization
from app.models.user import User
from app.services.mcp_server.fastmcp_app import verify_mcp_context


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[
        Organization.__table__, AgentApp.__table__, User.__table__])
    s = sessionmaker(bind=eng)()
    s.add(Organization(id="org-ok", name="ACME", slug="acme"))
    s.add(AgentApp(id="app-1", name="Analyst", org_id="org-ok"))
    s.add(User(id="u1", org_id="org-ok", email="a@t", full_name="A",
               role="user", password_hash="x"))
    s.commit()
    yield s
    s.close()
    eng.dispose()


def test_valid_claims_are_verified(db):
    ctx = verify_mcp_context(db, {
        "org_id": "org-ok", "agent_app_id": "app-1", "user_id": "u1",
        "run_id": "r1", "conversation_id": "c1"})
    assert ctx is not None
    assert ctx["org_id"] == "org-ok"
    assert ctx["verified"] is True  # stamped: downstream code can trust it


def test_nonexistent_org_drops_whole_context(db):
    assert verify_mcp_context(db, {
        "org_id": "org-ghost", "agent_app_id": "app-1"}) is None


def test_agent_from_other_org_drops_whole_context(db):
    from app.models.agent_app import AgentApp as A
    db.add(A(id="app-2", name="Rival", org_id="org-other"))
    db.commit()
    assert verify_mcp_context(db, {
        "org_id": "org-ok", "agent_app_id": "app-2"}) is None


def test_nonexistent_agent_drops_whole_context(db):
    assert verify_mcp_context(db, {
        "org_id": "org-ok", "agent_app_id": "app-ghost"}) is None


def test_deleted_agent_drops_whole_context(db):
    from app.models.agent_app import AgentApp as A
    a = db.get(A, "app-1")
    a.is_deleted = True
    db.commit()
    assert verify_mcp_context(db, {
        "org_id": "org-ok", "agent_app_id": "app-1"}) is None


def test_user_from_other_org_drops_whole_context(db):
    from app.models.user import User as U
    db.add(U(id="u-x", org_id="org-other", email="x@t", full_name="X",
             role="user", password_hash="x"))
    db.commit()
    assert verify_mcp_context(db, {
        "org_id": "org-ok", "user_id": "u-x"}) is None


def test_org_without_org_id_claim_is_not_verified(db):
    # no org claim → nothing to verify → NOT verified (fail-honest)
    ctx = verify_mcp_context(db, {"run_id": "r1"})
    assert ctx == {"run_id": "r1", "verified": False}


def test_context_without_any_identity_claims_is_rejected(db):
    assert verify_mcp_context(db, None) is None
    assert verify_mcp_context(db, "org-ok") is None
