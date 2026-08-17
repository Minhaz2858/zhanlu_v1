"""Tests for GET/PATCH /api/users/me/alert-preferences."""

import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.database import get_db
from app.deps import get_current_user_required
from app.routers.alert_preferences import router


class _Query:
    def __init__(self, db):
        self.db = db

    def filter(self, *a, **k):
        return self

    def first(self):
        return self.db.setting


class _FakeDB:
    def __init__(self, setting=None):
        self.setting = setting
        self.committed = False
        self.flushed = False

    def query(self, *a, **k):
        return _Query(self)

    def add(self, obj):
        self.setting = obj

    def flush(self):
        self.flushed = True

    def commit(self):
        self.committed = True

    def refresh(self, obj):
        return obj


def _make_client(setting=None):
    app = FastAPI()
    app.include_router(router)
    db = _FakeDB(setting)

    app.dependency_overrides[get_current_user_required] = lambda: SimpleNamespace(
        id="user-1", email="a@b.c", full_name="Tester"
    )
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), db


# GET default snapshot


def test_get_defaults_when_no_setting_row():
    client, db = _make_client(setting=None)
    resp = client.get("/users/me/alert-preferences")
    assert resp.status_code == 200
    data = resp.json()
    assert data["min_severity"] == "low"
    assert data["muted_products"] == []
    assert data["channels"] == ["in_app"]
    assert data["threshold_multiplier"] == 1.0
    assert data["quiet_hours"] is None
    assert data["recommendations_enabled"] is True
    assert data["webhook_url"] is None
    assert data["webhook_secret"] is None
    # a setting row was created for the user
    assert db.setting is not None


# PATCH partial updates


def test_patch_partial_min_severity():
    client, _ = _make_client(setting=None)
    resp = client.patch(
        "/users/me/alert-preferences", json={"min_severity": "high"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["min_severity"] == "high"
    # untouched fields keep defaults
    assert data["channels"] == ["in_app"]
    assert data["threshold_multiplier"] == 1.0


def test_patch_roundtrips_mute_list_and_multiplier():
    client, _ = _make_client(setting=None)
    resp = client.patch(
        "/users/me/alert-preferences",
        json={"muted_products": ["p1", "p2"], "threshold_multiplier": 1.5},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["muted_products"] == ["p1", "p2"]
    assert data["threshold_multiplier"] == 1.5


def test_patch_webhook_url_autogenerates_secret():
    client, _ = _make_client(setting=None)
    resp = client.patch(
        "/users/me/alert-preferences",
        json={"webhook_url": "https://hooks.example.com/x"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["webhook_url"] == "https://hooks.example.com/x"
    assert data["webhook_secret"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{20,}", data["webhook_secret"])


def test_patch_keeps_existing_secret():
    from app.models.user_setting import UserSetting

    setting = UserSetting(created_by_id="user-1", alert_webhook_secret="existing-secret")
    client, _ = _make_client(setting=setting)
    resp = client.patch(
        "/users/me/alert-preferences",
        json={"webhook_url": "https://hooks.example.com/y"},
    )
    assert resp.status_code == 200
    assert resp.json()["webhook_secret"] == "existing-secret"


def test_patch_quiet_hours():
    client, _ = _make_client(setting=None)
    resp = client.patch(
        "/users/me/alert-preferences",
        json={"quiet_hours": {"enabled": True, "start": "22:00", "end": "07:00"}},
    )
    assert resp.status_code == 200
    assert resp.json()["quiet_hours"]["enabled"] is True


# malformed input → 422


def test_patch_malformed_json_returns_422():
    client, _ = _make_client(setting=None)
    resp = client.patch(
        "/users/me/alert-preferences",
        json={"threshold_multiplier": "not-a-number"},
    )
    assert resp.status_code == 422


def test_patch_out_of_range_multiplier_returns_422():
    client, _ = _make_client(setting=None)
    resp = client.patch(
        "/users/me/alert-preferences",
        json={"threshold_multiplier": 99.0},
    )
    assert resp.status_code == 422


def test_patch_unknown_field_returns_422():
    client, _ = _make_client(setting=None)
    resp = client.patch(
        "/users/me/alert-preferences",
        json={"not_a_field": True},
    )
    assert resp.status_code == 422


# auth required


def test_requires_auth():
    app = FastAPI()
    app.include_router(router)

    def _reject():
        raise HTTPException(status_code=401, detail="Not authenticated")

    app.dependency_overrides[get_current_user_required] = _reject
    app.dependency_overrides[get_db] = lambda: _FakeDB()
    client = TestClient(app)
    resp = client.get("/users/me/alert-preferences")
    assert resp.status_code == 401
