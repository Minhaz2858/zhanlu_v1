"""Chat share API tests (Kimi/GPT-style conversation sharing).

Management endpoints (authed):
  POST   /api/chat/shares  {session_id} → {token, share_url}
  DELETE /api/chat/shares/{session_id}  → revoke

Public endpoints (NO auth):
  GET /share/c/{token}       → read-only HTML page
  GET /share/c/{token}/data  → {session_title, created_date, messages}
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from main import app
from app.database import SessionLocal, engine, Base
from app.models.user import User
from app.models.chat_session import ChatSession
from app.models.chat_message import ChatMessage
from app.services.auth_service import auth_service

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


def _uid() -> str:
    return str(uuid4())


def _now():
    return datetime.now(timezone.utc)


def _make_user(db, email="test@example.com", role="user", name="Test"):
    u = User(
        id=_uid(),
        email=email,
        full_name=name,
        role=role,
        password_hash=auth_service.hash_password("pwd"),
        created_date=_now(),
        updated_date=_now(),
        org_id="default-org",
        app_id="default-app",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _auth_headers(db, user):
    token = auth_service.create_access_token(user.id, db)
    return {"Authorization": f"Bearer {token}"}


def _make_session(db, user, title="Shared chat", agent_name="general_assistant"):
    s = ChatSession(
        id=_uid(),
        title=title,
        agent_name=agent_name,
        created_by_id=user.id,
        created_date=_now(),
        updated_date=_now(),
        org_id="default-org",
        app_id="default-app",
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _make_message(db, session_id, role, content, order=1):
    m = ChatMessage(
        id=_uid(),
        session_id=session_id,
        role=role,
        content=content,
        order=order,
        created_date=_now(),
        updated_date=_now(),
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def test_create_share_returns_token_and_public_url(db):
    user = _make_user(db)
    s = _make_session(db, user, title="Q3 Review")
    _make_message(db, s.id, "user", "summarize q3", order=1)
    _make_message(db, s.id, "assistant", "q3 grew 12%", order=2)

    r = client.post("/api/chat/shares", json={"session_id": s.id}, headers=_auth_headers(db, user))
    assert r.status_code == 200
    body = r.json()
    assert body["token"]
    assert body["share_url"] == f"/share/c/{body['token']}"
    assert len(body["token"]) >= 32


def test_public_data_route_returns_messages_without_auth(db):
    user = _make_user(db)
    s = _make_session(db, user, title="Q3 Review")
    _make_message(db, s.id, "user", "summarize q3", order=1)
    _make_message(db, s.id, "assistant", "q3 grew 12%", order=2)
    token = client.post("/api/chat/shares", json={"session_id": s.id}, headers=_auth_headers(db, user)).json()["token"]

    r = client.get(f"/share/c/{token}/data")
    assert r.status_code == 200
    body = r.json()
    assert body["session_title"] == "Q3 Review"
    assert len(body["messages"]) == 2
    assert body["messages"][0]["content"] == "summarize q3"
    assert body["messages"][1]["role"] == "assistant"


def test_public_data_route_404_for_unknown_token(db):
    r = client.get("/share/c/deadbeefdeadbeefdeadbeef/data")
    assert r.status_code == 404


def test_public_html_page_renders(db):
    user = _make_user(db)
    s = _make_session(db, user, title="Q3 Review")
    _make_message(db, s.id, "user", "hi", order=1)
    token = client.post("/api/chat/shares", json={"session_id": s.id}, headers=_auth_headers(db, user)).json()["token"]

    r = client.get(f"/share/c/{token}")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_create_share_reuses_existing_token(db):
    user = _make_user(db)
    s = _make_session(db, user, title="Once")
    h = _auth_headers(db, user)
    t1 = client.post("/api/chat/shares", json={"session_id": s.id}, headers=h).json()["token"]
    t2 = client.post("/api/chat/shares", json={"session_id": s.id}, headers=h).json()["token"]
    assert t1 == t2


def test_revoke_share_then_public_404(db):
    user = _make_user(db)
    s = _make_session(db, user, title="Revoke me")
    _make_message(db, s.id, "user", "secret", order=1)
    token = client.post("/api/chat/shares", json={"session_id": s.id}, headers=_auth_headers(db, user)).json()["token"]

    r = client.delete(f"/api/chat/shares/{s.id}", headers=_auth_headers(db, user))
    assert r.status_code == 200
    assert client.get(f"/share/c/{token}/data").status_code == 404


def test_cannot_share_another_users_session(db):
    alice = _make_user(db, email="alice@example.com")
    bob = _make_user(db, email="bob@example.com")
    bob_s = _make_session(db, bob, title="Bob private")

    r = client.post("/api/chat/shares", json={"session_id": bob_s.id}, headers=_auth_headers(db, alice))
    assert r.status_code in (403, 404)


def test_create_share_requires_auth(db):
    user = _make_user(db)
    s = _make_session(db, user, title="Authed only")
    r = client.post("/api/chat/shares", json={"session_id": s.id})
    assert r.status_code in (401, 403)
