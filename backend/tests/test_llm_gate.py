"""Tests for the golden-eval regression gate in the LLM admin endpoint (F3).

When EVAL_GATE_ENABLED, an admin PUT that changes the effective model
(model_id / provider / base_url / api_key / enabled / is_default) must pass
the golden suite at champion parity or the row is rejected with 409 BEFORE
commit. force=true bypasses. Name-only changes skip the gate.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.database import Base, engine, SessionLocal
from app.deps import get_current_user_required, get_db
from app.models.llm_model import LlmModel
from app.models.user import User
from app.routers.llm import router as llm_router
import app.models  # noqa: F401  register all models


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture
def db():
    s = SessionLocal()
    s.query(LlmModel).delete()
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _make_client(db, user):
    app = FastAPI()
    app.include_router(llm_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: user
    return TestClient(app)


def _seed_user(db, role="admin"):
    u = User(
        id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@t.io",
        full_name="t", role=role, password_hash="x",
        org_id="default-org", app_id="default-app",
    )
    db.add(u)
    db.commit()
    return u


def _seed_model(db, **overrides):
    m = LlmModel(
        name="Current", model_id="deepseek-chat", provider="deepseek",
        base_url="https://api.deepseek.com/v1", api_key="plaintext-key",
        enabled=True, is_default=True,
    )
    for k, v in overrides.items():
        setattr(m, k, v)
    db.add(m)
    db.commit()
    return m


def _fail_report():
    return {
        "status": "fail",
        "candidate": {"model": "candidate-1", "n": 6, "pass_rate": 0.5, "mean_completeness": 0.4},
        "champion": {"model": "deepseek-chat", "n": 6, "pass_rate": 0.9, "mean_completeness": 0.9},
        "cases": [], "regressed_cases": [],
    }


def _pass_report():
    return {
        "status": "pass",
        "candidate": {"model": "candidate-1", "n": 6, "pass_rate": 0.9, "mean_completeness": 0.9},
        "champion": {"model": "deepseek-chat", "n": 6, "pass_rate": 0.9, "mean_completeness": 0.9},
        "cases": [], "regressed_cases": [],
    }


async def _fake_run_golden_suite(db, **kwargs):
    """Default fake — tests override via monkeypatch."""
    return _pass_report()


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setattr(settings, "EVAL_GATE_ENABLED", True)
    monkeypatch.setattr(
        "app.services.golden_eval_runner.run_golden_suite", _fake_run_golden_suite
    )
    yield


def test_non_admin_gets_403(db):
    user = _seed_user(db, role="user")
    model = _seed_model(db)
    client = _make_client(db, user)
    r = client.put(f"/api/llm/models/{model.id}", json={"enabled": False})
    assert r.status_code == 403


def test_gate_blocks_failed_candidate_with_409(db, gate_on, monkeypatch):
    user = _seed_user(db, role="admin")
    model = _seed_model(db)

    async def fail_run(db, **kwargs):
        return _fail_report()

    monkeypatch.setattr("app.services.golden_eval_runner.run_golden_suite", fail_run)
    client = _make_client(db, user)

    r = client.put(f"/api/llm/models/{model.id}", json={"enabled": False})
    assert r.status_code == 409
    body = r.json()
    assert "golden-eval regression gate" in body["detail"]
    assert "0.5" in body["detail"]   # candidate pass rate surfaced
    assert "0.9" in body["detail"]   # champion pass rate surfaced
    assert "force=true" in body["detail"]

    # Rejected BEFORE commit — the row is unchanged in the DB.
    db.expire_all()
    fresh = db.query(LlmModel).filter(LlmModel.id == model.id).first()
    assert fresh.enabled is True
    assert fresh.base_url == "https://api.deepseek.com/v1"


def test_gate_allows_passing_candidate(db, gate_on, monkeypatch):
    user = _seed_user(db, role="admin")
    model = _seed_model(db)
    captured = {}

    async def capture_run(db, **kwargs):
        captured["endpoint"] = kwargs["endpoint"]
        return _pass_report()

    monkeypatch.setattr("app.services.golden_eval_runner.run_golden_suite", capture_run)
    client = _make_client(db, user)

    r = client.put(f"/api/llm/models/{model.id}", json={"model_id": "new-model-7"})
    assert r.status_code == 200
    assert r.json()["model_id"] == "new-model-7"
    # The gate must have been exercised against the MERGED candidate —
    # the body's new model_id, not the stale row value.
    assert captured["endpoint"].model_id == "new-model-7"


def test_gate_force_bypasses(db, gate_on, monkeypatch):
    user = _seed_user(db, role="admin")
    model = _seed_model(db)

    async def fail_run(db, **kwargs):
        return _fail_report()

    monkeypatch.setattr("app.services.golden_eval_runner.run_golden_suite", fail_run)
    client = _make_client(db, user)

    r = client.put(f"/api/llm/models/{model.id}", json={"enabled": False, "force": True})
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_gate_skipped_when_disabled(db, monkeypatch):
    monkeypatch.setattr(settings, "EVAL_GATE_ENABLED", False)

    async def should_not_be_called(db, **kwargs):
        raise AssertionError("gate must not run when disabled")

    monkeypatch.setattr("app.services.golden_eval_runner.run_golden_suite", should_not_be_called)
    user = _seed_user(db, role="admin")
    model = _seed_model(db)
    client = _make_client(db, user)

    r = client.put(f"/api/llm/models/{model.id}", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_gate_skipped_for_name_only_change(db, gate_on, monkeypatch):
    async def should_not_be_called(db, **kwargs):
        raise AssertionError("name-only changes must skip the gate")

    monkeypatch.setattr("app.services.golden_eval_runner.run_golden_suite", should_not_be_called)
    user = _seed_user(db, role="admin")
    model = _seed_model(db)
    client = _make_client(db, user)

    r = client.put(f"/api/llm/models/{model.id}", json={"name": "Renamed"})
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"


def test_unknown_model_404(db, gate_on):
    user = _seed_user(db, role="admin")
    client = _make_client(db, user)
    r = client.put("/api/llm/models/nope", json={"enabled": False})
    assert r.status_code == 404
