"""Phase 8.2 Stage 2 — external MCP authentication (RED first).

`authenticate_mcp_request` resolves the caller's identity from a Bearer
JWT using the EXISTING auth machinery (signature, expiry, JTI revocation
blacklist) and loads the authoritative User row. `is_privileged_jsonrpc`
separates public protocol negotiation from privileged tool execution.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.organization import Organization
from app.models.revoked_token import RevokedToken
from app.models.user import User
from app.services.auth_service import AuthService
from app.services.mcp_server.mcp_auth import (
    authenticate_mcp_request,
    is_privileged_jsonrpc,
)

svc_auth = AuthService()
ORG = "org-acme"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[
        Organization.__table__, User.__table__, RevokedToken.__table__])
    s = sessionmaker(bind=eng)()
    s.add(Organization(id=ORG, name="ACME", slug="acme"))
    s.add(User(id="u1", org_id=ORG, email="a@t", full_name="A",
               role="user", password_hash="x"))
    s.commit()
    yield s
    s.close()
    eng.dispose()


def _token(db):
    return svc_auth.create_access_token("u1", db)


def test_valid_token_authenticates(db):
    result = authenticate_mcp_request(db, f"Bearer {_token(db)}")
    assert result is not None
    user, claims = result
    assert user.id == "u1"
    assert user.org_id == ORG  # identity resolved from the DB row


def test_missing_header_fails(db):
    assert authenticate_mcp_request(db, None) is None


def test_malformed_header_fails(db):
    assert authenticate_mcp_request(db, "Bearer") is None
    assert authenticate_mcp_request(db, "Token abc") is None
    assert authenticate_mcp_request(db, "") is None


def test_garbage_token_fails(db):
    assert authenticate_mcp_request(db, "Bearer not-a-jwt") is None


def test_expired_token_fails(db, monkeypatch):
    import app.services.auth_service as asvc
    monkeypatch.setattr(asvc.settings, "ACCESS_TOKEN_EXPIRE_MINUTES", -1)
    assert authenticate_mcp_request(db, f"Bearer {_token(db)}") is None


def test_revoked_token_fails(db):
    token = _token(db)
    import jwt as pyjwt
    from app.config import settings
    payload = pyjwt.decode(token, settings.JWT_SECRET,
                           algorithms=[settings.JWT_ALGORITHM])
    svc_auth.revoke_access_token(
        payload["jti"], "u1",
        __import__("datetime").datetime.utcnow(), db)
    assert authenticate_mcp_request(db, f"Bearer {token}") is None


def test_deleted_user_fails(db):
    token = _token(db)
    u = db.get(User, "u1")
    u.is_deleted = True
    db.commit()
    assert authenticate_mcp_request(db, f"Bearer {token}") is None


def test_privileged_vs_public_methods():
    assert is_privileged_jsonrpc(
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{}}') is True
    assert is_privileged_jsonrpc(
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}') is False
    assert is_privileged_jsonrpc(
        b'{"jsonrpc":"2.0","method":"notifications/initialized"}') is False
    assert is_privileged_jsonrpc(
        b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}') is False
    assert is_privileged_jsonrpc(b"not json") is False
    assert is_privileged_jsonrpc(b"") is False


def test_batched_privileged_call_is_privileged(db):
    assert is_privileged_jsonrpc(
        b'[{"jsonrpc":"2.0","id":1,"method":"tools/list"},'
        b'{"jsonrpc":"2.0","id":2,"method":"tools/call"}]') is True
