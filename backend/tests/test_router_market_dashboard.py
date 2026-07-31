"""market_dashboard router — endpoint logic with auth overridden."""
import pytest
from fastapi.testclient import TestClient

from app.deps import get_current_user_required


@pytest.fixture
def client():
    """TestClient with auth overridden — tests endpoint logic, not auth."""
    from main import app
    fake_user = type("FakeUser", (), {
        "id": "test-user",
        "org_id": "default-org",
        "is_anonymous": False,
    })()
    app.dependency_overrides[get_current_user_required] = lambda: fake_user
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_enabled_returns_disabled_when_unconfigured(monkeypatch, client):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL", ""
    )
    monkeypatch.setattr(
        "app.services.market_dashboard_service.is_edia_reachable_cached",
        lambda: False,
    )
    r = client.get("/api/market-dashboard/enabled")
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is False
    assert data["reachable"] is False
    assert data["reason"] == "not_configured"


def test_snapshot_returns_503_when_unconfigured(monkeypatch, client):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL", ""
    )
    r = client.get("/api/market-dashboard/snapshot?group=upstream")
    assert r.status_code == 503
    assert r.json()["detail"] == "edia_unavailable"


def test_history_returns_503_when_unreachable(monkeypatch, client):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x",
    )
    monkeypatch.setattr(
        "app.services.market_dashboard_service.is_edia_reachable_cached",
        lambda: False,
    )
    r = client.get("/api/market-dashboard/history/naphtha?days=30")
    assert r.status_code == 503
    assert r.json()["detail"] == "edia_unreachable"


def test_regional_endpoint_503_when_unconfigured(monkeypatch, client):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL", ""
    )
    r = client.get("/api/market-dashboard/regional/naphtha")
    assert r.status_code == 503
    assert r.json()["detail"] == "edia_unavailable"


def test_news_endpoint_returns_empty_with_note(monkeypatch, client):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x",
    )
    monkeypatch.setattr(
        "app.services.market_dashboard_service.is_edia_reachable_cached",
        lambda: True,
    )
    r = client.get("/api/market-dashboard/news?group=upstream&limit=18")
    assert r.status_code == 200
    data = r.json()
    assert data["items"] == []
    assert "AI event pipeline" in (data["note"] or "")
