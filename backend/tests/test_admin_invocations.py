"""Tests for the admin agent-observability surface (2026-08-29).

Covers: invocation list + filters, summary aggregation (status/type/agent/
cost/latency/daily), detail with children/parent linkage, per-conversation
view, and admin RBAC (non-admin gets 403).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal, get_db
from app.deps import get_current_user_required
from app.models.agent_invocation import AgentInvocation
from app.models.user import User
from app.routers.admin_invocations import router as admin_invocations_router
import app.models  # noqa: F401  register all models


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture
def db():
    s = SessionLocal()
    s.query(AgentInvocation).delete()
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _make_client(db, user):
    app = FastAPI()
    app.include_router(admin_invocations_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: user
    return TestClient(app)


def _seed_user(db, role="user"):
    u = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@t.io",
             full_name="t", role=role, password_hash="x",
             org_id="default-org", app_id="default-app")
    db.add(u)
    db.commit()
    return u


def _seed_invocations(db):
    base = datetime.now(timezone.utc)
    rows = [
        AgentInvocation(
            agent_app_id="agent-1", conversation_id="conv-1", user_id="u1",
            invocation_type="conversation", status="completed",
            input_message="hello", assistant_content="hi there",
            duration_ms=1200, cost_amount=0.0042,
            token_usage={"prompt_tokens": 100, "completion_tokens": 50},
            confidence_score=0.9, model_name="deepseek-chat", tool_call_count=2,
            created_date=base,
        ),
        AgentInvocation(
            agent_app_id="agent-1", conversation_id="conv-1", user_id="u1",
            invocation_type="sub_agent", status="failed",
            input_message="sub task", error_message="boom",
            duration_ms=800, cost_amount=0.0011,
            confidence_score=None, model_name="deepseek-chat", tool_call_count=None,
            span_id=None, created_date=base - timedelta(hours=1),
        ),
        AgentInvocation(
            agent_app_id="agent-2", conversation_id="conv-2", user_id="u2",
            invocation_type="conversation", status="completed",
            input_message="another", assistant_content="reply",
            duration_ms=300, cost_amount=None,
            confidence_score=0.7, model_name="local-vllm", tool_call_count=0,
            created_date=base - timedelta(days=2),
        ),
    ]
    db.add_all(rows)
    db.commit()
    return rows


def test_non_admin_gets_403(db):
    user = _seed_user(db, role="user")
    client = _make_client(db, user)
    assert client.get("/api/admin/invocations").status_code == 403
    assert client.get("/api/admin/invocations/summary").status_code == 403


def test_admin_lists_invocations_newest_first(db):
    user = _seed_user(db, role="admin")
    _seed_invocations(db)
    client = _make_client(db, user)
    r = client.get("/api/admin/invocations")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 3
    assert data["items"][0]["conversation_id"] == "conv-1"
    assert data["items"][0]["model_name"] == "deepseek-chat"
    assert data["items"][0]["tool_call_count"] == 2


def test_list_filters(db):
    user = _seed_user(db, role="admin")
    _seed_invocations(db)
    client = _make_client(db, user)
    r = client.get("/api/admin/invocations", params={"status": "failed"})
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["error_message"] == "boom"

    r = client.get("/api/admin/invocations", params={"invocation_type": "sub_agent"})
    assert r.json()["total"] == 1

    r = client.get("/api/admin/invocations", params={"agent_app_id": "agent-2"})
    assert r.json()["total"] == 1

    r = client.get("/api/admin/invocations", params={"conversation_id": "conv-1"})
    assert r.json()["total"] == 2

    r = client.get("/api/admin/invocations", params={"limit": 2})
    assert r.json()["count"] == 2
    assert r.json()["total"] == 3


def test_list_invalid_date_rejected(db):
    user = _seed_user(db, role="admin")
    client = _make_client(db, user)
    assert client.get("/api/admin/invocations", params={"date_from": "not-a-date"}).status_code == 422


def test_summary_aggregation(db):
    user = _seed_user(db, role="admin")
    _seed_invocations(db)
    client = _make_client(db, user)
    r = client.get("/api/admin/invocations/summary")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 3
    assert data["by_status"] == {"completed": 2, "failed": 1}
    assert data["by_type"] == {"conversation": 2, "sub_agent": 1}
    assert round(data["totals"]["sum_cost"], 4) == 0.0053
    assert data["totals"]["turns_with_cost"] == 2
    assert data["totals"]["turns_with_model"] == 3
    assert round(data["totals"]["success_rate"], 3) == round(2 / 3, 3)
    assert data["by_agent"]["agent-1"]["count"] == 2
    assert round(data["by_agent"]["agent-1"]["sum_cost"], 4) == 0.0053
    assert data["by_agent"]["agent-1"]["avg_tool_calls"] == 2.0
    # base and base-1h land on the same day → 2 distinct dates.
    assert len(data["daily"]) == 2


def test_summary_empty_db(db):
    user = _seed_user(db, role="admin")
    client = _make_client(db, user)
    data = client.get("/api/admin/invocations/summary").json()
    assert data["total"] == 0
    assert data["totals"]["sum_cost"] == 0.0
    assert data["totals"]["avg_confidence"] is None


def test_detail_with_children_and_parent(db):
    user = _seed_user(db, role="admin")
    rows = _seed_invocations(db)
    parent, child, _ = rows
    child.span_id = parent.id
    db.commit()
    client = _make_client(db, user)
    r = client.get(f"/api/admin/invocations/{parent.id}")
    assert r.status_code == 200
    data = r.json()
    assert data["input_message"] == "hello"
    assert data["assistant_content"] == "hi there"
    assert data["token_usage"]["prompt_tokens"] == 100
    assert len(data["children"]) == 1
    assert data["children"][0]["id"] == child.id
    assert data["parent"] is None

    r = client.get(f"/api/admin/invocations/{child.id}")
    assert r.json()["parent"]["id"] == parent.id


def test_detail_404(db):
    user = _seed_user(db, role="admin")
    client = _make_client(db, user)
    assert client.get("/api/admin/invocations/nope").status_code == 404


def test_conversation_view(db):
    user = _seed_user(db, role="admin")
    _seed_invocations(db)
    client = _make_client(db, user)
    r = client.get("/api/admin/invocations/conversations/conv-1")
    assert r.status_code == 200
    assert r.json()["count"] == 2
