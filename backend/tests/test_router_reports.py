"""Integration tests for reports router endpoints.

Tests: 503 when EDIA unavailable, 400 for unknown org, 200 response model.
Uses TestClient with overridden auth dependency.
"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from main import app


# Minimal auth override injected via main.py's dependency
AUTH_OVERRIDE = {}

client = TestClient(app)

# A valid auth header that our test auth override accepts
HEADERS = {"Authorization": "Bearer test-admin-token"}


# ── Helpers ──────────────────────────────────────────────────────────────


def mock_exec_rows(org: str = "huizhou"):
    """Return synthetic normalized rows for load_contract_execution."""
    return (
        [
            {
                "ORG_NAME": "惠州伊斯科" if org == "huizhou" else "广东伊斯科",
                "FDATE": "2025-07-15",
                "FBILLNO": "BILL-001",
                "CUST_NAME": "青岛橡塑科技",
                "material_name": "C5石油树脂",
                "product_type": "resin",
                "grade": "A",
                "FDELIVERYDATE": "2025-08-01",
                "contract_fqty": 200.0,
                "contract_amount": 1000000.0,
                "contract_notdofqty": 50.0,
                "ORDERQTY": 200.0,
                "ORDERAMOUNT": 1000000.0,
                "ftaxprice_hit_count": 1,
                "fprice_hit_count": 0,
                "order_tax_unit_price": 5000.0,
                "plan_fqty": 0,
                "out_fqty": 150.0,
                "out_amount": 750000.0,
                "out_notoutqty": 50.0,
                "material_unit": "吨",
                "currency": "CNY",
            }
        ],
        {"key": org, "label": "惠州伊斯科" if org == "huizhou" else "广东伊斯科"},
    )


# ── Auth override ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def patch_auth():
    """Override auth dependency for all tests."""
    async def fake_user():
        return {"id": "test-user", "email": "admin@zhanlu.dev"}

    app.dependency_overrides["get_current_user_required"] = fake_user
    # Avoid dependency name miss: find the actual function ref
    from app.deps import get_current_user_required
    app.dependency_overrides[get_current_user_required] = fake_user
    yield
    app.dependency_overrides.clear()


# ── 503 when EDIA is unreachable ────────────────────────────────────────

def test_customer_sales_503_when_edia_unreachable():
    with patch("app.routers.reports.is_edia_reachable", return_value=False):
        resp = client.get(
            "/api/reports/customer-sales?org=huizhou", headers=HEADERS,
        )
        assert resp.status_code == 503
        assert "edia_unavailable" in resp.json()["detail"]


def test_contract_performance_503_when_edia_unreachable():
    with patch("app.routers.reports.is_edia_reachable", return_value=False):
        resp = client.get(
            "/api/reports/contract-performance?org=huizhou", headers=HEADERS,
        )
        assert resp.status_code == 503


# ── 400 unknown org ──────────────────────────────────────────────────────

def test_customer_sales_400_unknown_org():
    with patch("app.services.report_service.get_mysql_engine"):
        with patch("app.routers.reports.is_edia_reachable", return_value=True):
            resp = client.get(
                "/api/reports/customer-sales?org=shanghai", headers=HEADERS,
            )
            assert resp.status_code == 400
            assert "shanghai" in resp.json()["detail"] or "Unknown org" in resp.json()["detail"]


def test_contract_performance_400_unknown_org():
    with patch("app.services.report_service.get_mysql_engine"):
        with patch("app.routers.reports.is_edia_reachable", return_value=True):
            resp = client.get(
                "/api/reports/contract-performance?org=beijing", headers=HEADERS,
            )
            assert resp.status_code == 400


# ── 200 responses (with mocked MySQL) ────────────────────────────────────

def test_customer_sales_200():
    with patch(
        "app.services.report_service.load_contract_execution",
        return_value=mock_exec_rows("huizhou"),
    ):
        with patch("app.routers.reports.is_edia_reachable", return_value=True):
            resp = client.get(
                "/api/reports/customer-sales?org=huizhou&month=2025-07",
                headers=HEADERS,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["month"] == "2025-07"
            assert data["org_name"] == "惠州伊斯科"
            assert "summary" in data
            assert "customer_share" in data
            assert "price_chart" in data


def test_contract_performance_200():
    with patch(
        "app.services.report_service.load_contract_execution",
        return_value=mock_exec_rows("huizhou"),
    ):
        with patch("app.routers.reports.is_edia_reachable", return_value=True):
            resp = client.get(
                "/api/reports/contract-performance?org=huizhou"
                "&start_date=2025-07-01&end_date=2025-07-31",
                headers=HEADERS,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["org_name"] == "惠州伊斯科"
            assert "summary" in data
            assert "execution_chart" in data
            assert "unshipped_contracts" in data


def test_customer_sales_debug():
    with patch(
        "app.services.report_service.load_contract_execution",
        return_value=mock_exec_rows("huizhou"),
    ):
        with patch("app.routers.reports.is_edia_reachable", return_value=True):
            resp = client.get(
                "/api/reports/customer-sales?org=huizhou&month=2025-07&debug=true",
                headers=HEADERS,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "debug" in data
            assert "sample_rows" in data["debug"]


def test_ai_analysis_sse():
    with patch(
        "app.services.report_service.load_contract_execution",
        return_value=mock_exec_rows("huizhou"),
    ):
        with patch("app.routers.reports.is_edia_reachable", return_value=True):
            resp = client.get(
                "/api/reports/contract-performance/ai-analysis"
                "?org=huizhou&start_date=2025-07-01&end_date=2025-07-31",
                headers=HEADERS,
            )
            assert resp.status_code == 200
            assert resp.headers.get("content-type", "").startswith("text/event-stream")
