"""Weekly digest PPTX export route — flag-gate, 404, 503, and success."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.deps import get_current_user_required, get_db


class _FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *a):
        return self

    def order_by(self, *a):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, by_model=None):
        self._by_model = by_model or {}

    def query(self, model):
        name = getattr(model, "__name__", str(model))
        return _FakeQuery(self._by_model.get(name, []))


def _card(**overrides):
    base = dict(
        id="card-1",
        org_id="default-org",
        week_start=date(2026, 8, 10),
        week_end=date(2026, 8, 16),
        title_zh="周度总结及预测",
        title_en="Weekly Summary & Forecast",
        status="ready",
        error_msg=None,
        market_overview="overview",
        executive_summary="summary",
        generated_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def client(monkeypatch):
    from main import app

    monkeypatch.setattr(
        "app.routers.weekly_digest_api.settings.WEEKLY_DIGEST_ENABLED", True
    )
    fake_user = type("FakeUser", (), {
        "id": "test-user",
        "org_id": "default-org",
        "is_anonymous": False,
    })()
    app.dependency_overrides[get_current_user_required] = lambda: fake_user
    yield app
    app.dependency_overrides.clear()


def test_export_returns_503_when_flag_disabled(monkeypatch, client):
    monkeypatch.setattr(
        "app.routers.weekly_digest_api.settings.WEEKLY_DIGEST_PPTX_EXPORT_ENABLED",
        False,
    )
    db = _FakeDB({"WeeklyReportCard": [_card()], "WeeklyReportProduct": []})
    client.dependency_overrides[get_db] = lambda: db
    r = TestClient(client).post("/api/weekly-digest/card-1/export/pptx")
    assert r.status_code == 503
    assert "not enabled" in r.json()["detail"]


def test_export_returns_404_when_missing(monkeypatch, client):
    monkeypatch.setattr(
        "app.routers.weekly_digest_api.settings.WEEKLY_DIGEST_PPTX_EXPORT_ENABLED",
        True,
    )
    db = _FakeDB({"WeeklyReportCard": [], "WeeklyReportProduct": []})
    client.dependency_overrides[get_db] = lambda: db
    r = TestClient(client).post("/api/weekly-digest/missing/export/pptx")
    assert r.status_code == 404


def test_export_returns_503_when_generating(monkeypatch, client):
    monkeypatch.setattr(
        "app.routers.weekly_digest_api.settings.WEEKLY_DIGEST_PPTX_EXPORT_ENABLED",
        True,
    )
    db = _FakeDB({"WeeklyReportCard": [_card(status="generating")],
                  "WeeklyReportProduct": []})
    client.dependency_overrides[get_db] = lambda: db
    r = TestClient(client).post("/api/weekly-digest/card-1/export/pptx")
    assert r.status_code == 503
    assert "generated" in r.json()["detail"].lower()


def test_export_streams_pptx_with_disposition(monkeypatch, client):
    monkeypatch.setattr(
        "app.routers.weekly_digest_api.settings.WEEKLY_DIGEST_PPTX_EXPORT_ENABLED",
        True,
    )
    db = _FakeDB({"WeeklyReportCard": [_card()], "WeeklyReportProduct": []})
    client.dependency_overrides[get_db] = lambda: db
    r = TestClient(client).post("/api/weekly-digest/card-1/export/pptx")
    assert r.status_code == 200
    assert "presentationml" in r.headers["content-type"]
    assert 'attachment; filename="weekly_digest_2026-08-10_default-org.pptx"' in (
        r.headers["content-disposition"]
    )
    # .pptx is a ZIP — starts with PK
    assert r.content[:2] == b"PK"
