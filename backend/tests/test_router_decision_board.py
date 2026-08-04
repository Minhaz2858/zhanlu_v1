"""decision-board endpoint — auth overridden, tests endpoint logic."""
import pytest
from fastapi.testclient import TestClient

from app.deps import get_current_user_required


@pytest.fixture
def client():
    from main import app
    fake_user = type("FakeUser", (), {
        "id": "test-user", "org_id": "default-org", "is_anonymous": False,
    })()
    app.dependency_overrides[get_current_user_required] = lambda: fake_user
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_decision_board_503_when_unconfigured(monkeypatch, client):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL", ""
    )
    r = client.get("/api/market-dashboard/decision-board?group=midstream&day=7")
    assert r.status_code == 503
    assert r.json()["detail"] == "edia_unavailable"


def test_decision_board_returns_payload(monkeypatch, client):
    from app.schemas.market_dashboard import (
        DecisionBoardProduct, DecisionBoardResponse,
    )
    fake = DecisionBoardResponse(
        group="midstream", day=7, generated_at="2026-08-04T00:00:00+00:00",
        briefing_zh="未来 7 天市场整体平稳。",
        products=[DecisionBoardProduct(
            id="isoprene", name_zh="异戊二烯", name_en="Isoprene",
            code="isoprene",
        )],
    )
    monkeypatch.setattr(
        "app.services.decision_board_service.build_decision_board",
        lambda group, day: fake,
    )
    r = client.get("/api/market-dashboard/decision-board?group=midstream")
    assert r.status_code == 200
    data = r.json()
    assert data["products"][0]["name_zh"] == "异戊二烯"
    assert data["briefing_zh"]


def test_decision_board_rejects_bad_group(client):
    r = client.get("/api/market-dashboard/decision-board?group=bogus")
    assert r.status_code == 422
