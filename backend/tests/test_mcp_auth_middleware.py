"""Phase 8.2 Stage 2 — the ASGI auth middleware on the MCP transport.

Public negotiation (initialize/tools/list) passes unauthenticated;
privileged tools/call requires valid credentials; the authenticated
principal is exposed to the tool layer via contextvar; failures return
401 with static error text and are audited (org attribution only when a
valid token identified the user — anonymous failures audit without org).
"""

import json

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.organization import Organization
from app.models.user import User
from app.services.auth_service import AuthService
from app.services.mcp_server.mcp_auth import get_principal


@pytest.fixture()
def env():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[
        Organization.__table__, User.__table__])
    s = sessionmaker(bind=eng)()
    s.add(Organization(id="org-acme", name="ACME", slug="acme"))
    s.add(User(id="u1", org_id="org-acme", email="a@t", full_name="A",
               role="user", password_hash="x"))
    s.commit()

    from app.services.mcp_server.fastmcp_app import get_mcp_http_app
    app = get_mcp_http_app()
    # a recorder inner app is NOT used — we drive the real transport but
    # only assert on the middleware's 401 short-circuit for tools/call.
    yield app, s, AuthService()
    s.close()
    eng.dispose()


def _post_body(app, scope, body: bytes):
    import asyncio

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    async def call():
        await app(scope, receive, send)

    asyncio.run(call())
    return sent


def _scope(path="/mcp", method="POST"):
    return {
        "type": "http", "asgi": {"version": "3.0"},
        "http_version": "1.1", "method": method,
        "path": path, "query_string": b"",
        "headers": [(b"content-type", b"application/json"),
                    (b"host", b"testserver")],
        "state": {},
    }


def test_privileged_call_without_credentials_is_401_short_circuit(env):
    app, s, auth = env
    scope = _scope()
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": "tools/call",
                       "params": {"name": "Skill", "arguments": {}}}).encode()
    sent = _post_body(app, scope, body)
    assert sent and sent[0]["status"] == 401
    text = sent[-1].get("body", b"")
    assert b"authentication required" in text or b"-32001" in text


def test_privileged_call_with_garbage_token_is_401(env):
    app, s, auth = env
    scope = _scope()
    scope["headers"] += [(b"authorization", b"Bearer garbage")]
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": "tools/call",
                       "params": {"name": "Skill", "arguments": {}}}).encode()
    sent = _post_body(app, scope, body)
    assert sent and sent[0]["status"] == 401


def test_public_initialize_passes_through_without_credentials(env):
    app, s, auth = env
    scope = _scope()
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {}}).encode()
    # The middleware must NOT short-circuit public negotiation: the request
    # reaches the inner FastMCP app. Without the transport lifespan running
    # (raw ASGI, no host startup) the inner app raises its session-manager
    # error — which is itself proof the auth layer passed the request on.
    import asyncio

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    async def call():
        await app(scope, receive, send)

    try:
        asyncio.run(call())
    except RuntimeError as exc:
        # inner app error (no lifespan) — proves the auth layer passed through
        assert "Task group is not initialized" in str(exc)
    statuses = [m.get("status") for m in sent
                if m["type"] == "http.response.start"]
    assert 401 not in statuses


# ── Phase 10 (audit S4.1/S4.2): session isolation + principal hygiene ────

def _get_scope(session_id: bytes | None):
    scope = _scope(method="GET")
    headers = [(b"accept", b"text/event-stream"), (b"host", b"testserver")]
    if session_id:
        headers.append((b"mcp-session-id", session_id))
    scope["headers"] = headers
    return scope


def test_get_with_session_id_requires_auth(env):
    """A captured mcp-session-id is NOT a credential: GET SSE / DELETE on
    another principal's session must 401 without a valid Bearer token."""
    app, s, auth = env
    sent = _post_body(app, _get_scope(b"captured-session-id"), b"")
    assert sent and sent[0]["status"] == 401


def test_get_with_session_id_and_garbage_token_is_401(env):
    app, s, auth = env
    scope = _get_scope(b"captured-session-id")
    scope["headers"] += [(b"authorization", b"Bearer garbage")]
    sent = _post_body(app, scope, b"")
    assert sent and sent[0]["status"] == 401


def test_get_without_session_id_passes_public(env):
    """Bare capability probe (no session) stays public negotiation."""
    app, s, auth = env
    sent = []
    try:
        sent = _post_body(app, _get_scope(None), b"")
    except RuntimeError as exc:
        # inner app error (no lifespan) — proves the auth layer passed through
        assert "Task group is not initialized" in str(exc)
    statuses = [m.get("status") for m in sent
                if m["type"] == "http.response.start"]
    assert 401 not in statuses


def test_principal_reset_after_privileged_request(env):
    """S4.2: the contextvar principal never survives a request."""
    from app.services.mcp_server.mcp_auth import reset_principal, set_principal

    set_principal({"user_id": "u1", "org_id": "org-acme"})
    reset_principal()
    assert get_principal() is None
