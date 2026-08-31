"""Tests for the admin eval surface (Phase 0 agent-gaps build).

Covers: eval_results list endpoint, summary aggregation (dimensions,
by_model, by_verdict), golden-cases listing, and admin RBAC (non-admin
gets 403).
"""

from __future__ import annotations

import json
import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal, get_db
from app.deps import get_current_user_required
from app.models.eval_result import EvalResult
from app.models.user import User
from app.routers.admin_evals import router as admin_evals_router
import app.models  # noqa: F401  register all models


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture
def db():
    s = SessionLocal()
    # Per-test isolation: the shared sqlite file persists across tests in the
    # module, so clear the tables this suite touches before each test.
    from app.models.agent_test_case import AgentTestCase

    s.query(EvalResult).delete()
    s.query(AgentTestCase).delete()
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _make_client(db, user):
    app = FastAPI()
    app.include_router(admin_evals_router, prefix="/api")
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


def _seed_evals(db):
    db.add_all(
        [
            EvalResult(
                conversation_id="c1", user_message="q1", assistant_text="a1",
                scores=json.dumps({"completeness": 0.9, "accuracy": 0.8}),
                verdict="accept", model="deepseek-chat",
            ),
            EvalResult(
                conversation_id="c2", user_message="q2", assistant_text="a2",
                scores=json.dumps({"completeness": 0.5, "accuracy": 0.4}),
                verdict="reject", model="deepseek-chat",
            ),
            EvalResult(
                conversation_id="c3", user_message="q3", assistant_text="a3",
                scores=json.dumps({"completeness": 0.7}),
                verdict="accept", model="local-vllm",
            ),
        ]
    )
    db.commit()


def test_non_admin_gets_403(db):
    user = _seed_user(db, role="user")
    client = _make_client(db, user)
    assert client.get("/api/admin/evals").status_code == 403
    assert client.get("/api/admin/evals/summary").status_code == 403
    assert client.get("/api/admin/evals/cases").status_code == 403


def test_admin_lists_evals(db):
    user = _seed_user(db, role="admin")
    _seed_evals(db)
    client = _make_client(db, user)
    resp = client.get("/api/admin/evals")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3
    assert all(item["verdict"] in ("accept", "reject") for item in body["items"])
    # Equal created_date timestamps make row order nondeterministic on sqlite —
    # assert the SET of completeness scores rather than a fixed first row.
    completeness_scores = {item["scores"].get("completeness") for item in body["items"]}
    assert completeness_scores == {0.9, 0.5, 0.7}


def test_admin_summary_aggregates(db):
    user = _seed_user(db, role="admin")
    _seed_evals(db)
    client = _make_client(db, user)
    body = client.get("/api/admin/evals/summary").json()
    assert body["total"] == 3
    # 2 accepts out of 3
    assert body["pass_rate"] == round(2 / 3, 3)
    assert "completeness" in body["dimensions"]
    # completeness avg = (0.9 + 0.5 + 0.7) / 3
    assert body["dimensions"]["completeness"] == round(2.1 / 3, 3)
    assert body["by_verdict"]["accept"] == 2
    assert body["by_verdict"]["reject"] == 1
    assert "deepseek-chat" in body["by_model"]
    assert body["by_model"]["deepseek-chat"]["count"] == 2
    assert body["by_model"]["deepseek-chat"]["pass_rate"] == 0.5


def test_admin_summary_empty(db):
    user = _seed_user(db, role="admin")
    client = _make_client(db, user)
    body = client.get("/api/admin/evals/summary").json()
    assert body == {"total": 0, "pass_rate": 0.0, "dimensions": {}, "by_model": {}, "by_verdict": {}}


def test_admin_lists_cases(db):
    from app.models.agent_test_case import AgentTestCase

    user = _seed_user(db, role="admin")
    db.add(AgentTestCase(
        agent_app_id="app-1", name="sql-query", description="NL→SQL",
        test_type="integration", input_json={"user_message": "total sales?"},
        status="pending",
    ))
    db.commit()
    client = _make_client(db, user)
    body = client.get("/api/admin/evals/cases").json()
    assert body["count"] == 1
    assert body["items"][0]["name"] == "sql-query"
    assert body["items"][0]["input"]["user_message"] == "total sales?"
