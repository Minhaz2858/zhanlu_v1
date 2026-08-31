"""Chat history search endpoint tests.

GET /api/chat/search?q=... — user-scoped ILIKE across chat_messages joined
to chat_sessions. Returns the caller's sessions whose messages contain q,
grouped by session with a bounded snippet.
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


def _make_session(db, user, title="Chat A", agent_name="general_assistant"):
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


def test_search_returns_only_matching_sessions(db):
    user = _make_user(db)
    s_match = _make_session(db, user, title="Quarterly Report")
    s_other = _make_session(db, user, title="Unrelated")
    _make_message(db, s_match.id, "user", "The needle is in this haystack", order=1)
    _make_message(db, s_match.id, "assistant", "Found it", order=2)
    _make_message(db, s_other.id, "user", "Nothing to see here", order=1)

    r = client.get("/api/chat/search", params={"q": "needle"}, headers=_auth_headers(db, user))
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "needle"
    assert len(body["results"]) == 1
    res = body["results"][0]
    assert res["session_id"] == s_match.id
    assert res["title"] == "Quarterly Report"
    assert res["agent_name"] == "general_assistant"
    assert any("needle" in m["snippet"] for m in res["matches"])


def test_search_is_scoped_to_caller(db):
    alice = _make_user(db, email="alice@example.com")
    bob = _make_user(db, email="bob@example.com")
    bob_s = _make_session(db, bob, title="Bob's secrets")
    _make_message(db, bob_s.id, "user", "needle hidden in bob chat", order=1)

    r = client.get("/api/chat/search", params={"q": "needle"}, headers=_auth_headers(db, alice))
    assert r.status_code == 200
    assert r.json()["results"] == []


def test_search_escapes_like_wildcards(db):
    user = _make_user(db)
    s = _make_session(db, user, title="Percent chat")
    _make_message(db, s.id, "user", "discount is 100% off today", order=1)

    r = client.get("/api/chat/search", params={"q": "0%"}, headers=_auth_headers(db, user))
    assert r.status_code == 200
    assert len(r.json()["results"]) == 1

    r2 = client.get("/api/chat/search", params={"q": "%"}, headers=_auth_headers(db, user))
    assert r2.status_code == 200
    assert len(r2.json()["results"]) == 1


def test_search_blank_query_rejected(db):
    user = _make_user(db)
    r = client.get("/api/chat/search", params={"q": "   "}, headers=_auth_headers(db, user))
    assert r.status_code == 400


def test_search_snippet_is_bounded(db):
    user = _make_user(db)
    s = _make_session(db, user, title="Long doc")
    long_text = "alpha " * 200 + "needle" + " omega " * 200
    _make_message(db, s.id, "user", long_text, order=1)

    r = client.get("/api/chat/search", params={"q": "needle"}, headers=_auth_headers(db, user))
    body = r.json()
    assert len(body["results"]) == 1
    snip = body["results"][0]["matches"][0]["snippet"]
    assert "needle" in snip
    assert len(snip) <= 160


def test_search_excludes_soft_deleted_sessions(db):
    user = _make_user(db)
    s = _make_session(db, user, title="Deleted chat")
    _make_message(db, s.id, "user", "needle in deleted chat", order=1)
    s.is_deleted = True
    db.commit()

    r = client.get("/api/chat/search", params={"q": "needle"}, headers=_auth_headers(db, user))
    assert r.json()["results"] == []


def test_search_requires_auth():
    r = client.get("/api/chat/search", params={"q": "needle"})
    assert r.status_code in (401, 403)
