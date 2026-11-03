"""Phase 8.2 Stage 2 — MCP token issuance/revocation endpoint.

POST /api/mcp/tokens issues a long-lived personal MCP JWT (existing JWT
machinery + JTI revocation blacklist); POST /api/mcp/tokens/revoke revokes
by jti (only the owner's own tokens); issuance and revocation are audited.
The issued token authenticates against the MCP transport middleware.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.audit_event import AuditEvent
from app.models.organization import Organization
from app.models.revoked_token import RevokedToken
from app.models.user import User
from app.deps import get_current_user_required, get_db
from app.routers.mcp_tokens import router


@pytest.fixture()
def client():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[
        Organization.__table__, User.__table__, RevokedToken.__table__,
        AuditEvent.__table__])
    s = sessionmaker(bind=eng)()
    s.add(Organization(id="org-acme", name="ACME", slug="acme"))
    s.add(User(id="u1", org_id="org-acme", email="a@t", full_name="A",
               role="user", password_hash="x"))
    s.commit()

    app = FastAPI()
    app.include_router(router)

    state = {"user": "u1"}

    def _get_db():
        yield s

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user_required] = lambda: s.get(
        User, state["user"])
    c = TestClient(app)
    c.s = s
    c.state = state
    yield c
    s.close()
    eng.dispose()


def test_issue_and_use_mcp_token(client):
    res = client.post("/api/mcp/tokens", json={"days": 30})
    assert res.status_code == 200, res.text
    token = res.json()["token"]
    assert token
    # the issued token authenticates on the MCP transport machinery
    from app.services.mcp_server.mcp_auth import authenticate_mcp_request
    result = authenticate_mcp_request(client.s, f"Bearer {token}")
    assert result is not None
    user, claims = result
    assert user.id == "u1" and user.org_id == "org-acme"
    # issuance audited (no token material in details)
    ev = client.s.query(AuditEvent).filter_by(
        event_type="mcp_token_issued").one()
    assert ev.actor_id == "u1" and ev.org_id == "org-acme"
    assert token not in str(ev.detail_json)
    assert token not in str(client.s.query(AuditEvent).all())


def test_revoke_own_token(client):
    token = client.post("/api/mcp/tokens", json={"days": 30}).json()["token"]
    res = client.post("/api/mcp/tokens/revoke", json={"token": token})
    assert res.status_code == 200, res.text
    # revoked token no longer authenticates
    from app.services.mcp_server.mcp_auth import authenticate_mcp_request
    assert authenticate_mcp_request(
        client.s, f"Bearer {token}") is None


def test_cannot_revoke_another_users_token(client):
    token = client.post("/api/mcp/tokens", json={"days": 30}).json()["token"]
    # a DIFFERENT owner presents the raw token — signature is valid but the
    # subject is not the caller, so revocation is refused AND audited
    client.state["user"] = "u-other"
    client.s.add(User(id="u-other", org_id="org-acme", email="b@t",
                      full_name="B", role="user", password_hash="x"))
    client.s.commit()
    res = client.post("/api/mcp/tokens/revoke", json={"token": token})
    assert res.status_code == 403
    # token still works
    from app.services.mcp_server.mcp_auth import authenticate_mcp_request
    assert authenticate_mcp_request(
        client.s, f"Bearer {token}") is not None
