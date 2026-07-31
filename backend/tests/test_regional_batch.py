"""Tests for /api/market-dashboard/regional-batch endpoint (Task 1)."""
import pytest
from unittest.mock import patch
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


def test_regional_batch_returns_one_entry_per_product(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x", raising=False,
    )
    monkeypatch.setattr(
        "app.services.market_dashboard_service.is_edia_reachable_cached",
        lambda: True,
    )
    fake_data = {
        "crude_oil": {
            "product_id": "crude_oil", "regions": [], "spread_cny": None,
            "spread_level": "low", "price_realization_pct": None, "cost_floor": None,
        },
        "naphtha": {
            "product_id": "naphtha", "regions": [{
                "price_type": "x", "label": "x", "is_internal": False,
                "price": 100.0, "change_pct": None, "date": "2026-07-30",
                "days_stale": 0,
            }],
            "spread_cny": None, "spread_level": "low",
            "price_realization_pct": None, "cost_floor": None,
        },
    }
    with patch(
        "app.services.market_dashboard_service.get_regional_prices",
        side_effect=lambda pid: fake_data.get(pid, {"product_id": pid, "regions": []}),
    ):
        r = client.get("/api/market-dashboard/regional-batch?product_ids=crude_oil,naphtha")
    assert r.status_code == 200
    body = r.json()
    assert set(body["items"].keys()) == {"crude_oil", "naphtha"}
    assert body["items"]["naphtha"]["regions"][0]["price"] == 100.0


def test_regional_batch_captures_per_product_errors(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x", raising=False,
    )
    monkeypatch.setattr(
        "app.services.market_dashboard_service.is_edia_reachable_cached",
        lambda: True,
    )
    with patch(
        "app.services.market_dashboard_service.get_regional_prices",
        side_effect=RuntimeError("query timeout"),
    ):
        r = client.get("/api/market-dashboard/regional-batch?product_ids=crude_oil")
    assert r.status_code == 200
    body = r.json()
    assert "crude_oil" in body["errors"]
    assert "query timeout" in body["errors"]["crude_oil"]


def test_regional_batch_503_when_unconfigured(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL", "",
        raising=False,
    )
    r = client.get("/api/market-dashboard/regional-batch?product_ids=crude_oil")
    assert r.status_code == 503
    assert r.json()["detail"] == "edia_unavailable"
