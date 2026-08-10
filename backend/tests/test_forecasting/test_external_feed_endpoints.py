"""Tests for Wave 3 T3.0 external-feed API endpoints in forecast_ops router."""
from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.models.forecasting import (
    ForecastExternalPoint,
    ForecastExternalSeries,
)


@pytest.fixture(autouse=True)
def _schema_and_clean():
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        try:
            session.query(ForecastExternalPoint).delete()
        except Exception:
            pass
        try:
            session.query(ForecastExternalSeries).delete()
        except Exception:
            pass
        session.commit()
    finally:
        session.close()
    yield


def _make_user(role: str, user_id: str) -> SimpleNamespace:
    """Real attribute container (MagicMock's id attribute doesn't pass through SQLAlchemy)."""
    return SimpleNamespace(id=user_id, role=role)


# Build a minimal FastAPI app that mounts the forecast_ops router only —
# this avoids the heavy full-app import surface and lets us test the
# endpoints in isolation.
def _build_test_client(monkeypatch, user):
    """Construct a TestClient with the forecast_ops router mounted.

    Overrides get_db + auth dependencies so the test doesn't need a live DB
    beyond the conftest's in-memory SQLite, and doesn't need auth wired up.
    """
    from fastapi import FastAPI
    from app.routers.forecast_ops import router as forecast_ops_router
    from app.database import get_db
    from app.deps import get_current_user_required, require_admin

    app = FastAPI()
    app.include_router(forecast_ops_router, prefix="/api")

    def _override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user_required] = lambda: user
    # require_admin just returns the user if they're admin, else raises 403
    if user.role == "admin":
        app.dependency_overrides[require_admin] = lambda: user
    else:
        from fastapi import HTTPException as _HTTPException
        app.dependency_overrides[require_admin] = lambda: (
            _raise(_HTTPException(status_code=403, detail="Admin access required"))
        )

    return TestClient(app)


def _raise(exc):
    raise exc


# ------------------------------------------------------------------ #
# POST /external-feeds (upload)
# ------------------------------------------------------------------ #

class TestUploadEndpoint:

    def test_upload_returns_summary(self, monkeypatch):
        client = _build_test_client(monkeypatch, _make_user("user", "u-1"))
        csv = "date,value\n2025-01-01,70.0\n2025-01-08,72.0\n"
        resp = client.post(
            "/api/forecast-ops/external-feeds",
            params={"series_key": "op_rate_iso", "domain": "operating_rate",
                    "product_key": "isoprene", "unit": "%"},
            files={"file": ("test.csv", csv.encode("utf-8"), "text/csv")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["series_key"] == "op_rate_iso"
        assert body["row_count"] == 2
        assert body["rows_inserted"] == 2

    def test_upload_invalid_domain_400(self, monkeypatch):
        client = _build_test_client(monkeypatch, _make_user("user", "u-1"))
        csv = "date,value\n2025-01-01,70.0\n"
        resp = client.post(
            "/api/forecast-ops/external-feeds",
            params={"series_key": "x", "domain": "bogus"},
            files={"file": ("test.csv", csv.encode("utf-8"), "text/csv")},
        )
        assert resp.status_code == 400
        assert "domain" in resp.json()["detail"].lower()

    def test_upload_missing_date_column_400(self, monkeypatch):
        client = _build_test_client(monkeypatch, _make_user("user", "u-1"))
        csv = "when,value\n2025-01-01,70.0\n"
        resp = client.post(
            "/api/forecast-ops/external-feeds",
            params={"series_key": "x", "domain": "operating_rate"},
            files={"file": ("test.csv", csv.encode("utf-8"), "text/csv")},
        )
        assert resp.status_code == 400
        assert "date" in resp.json()["detail"].lower()


# ------------------------------------------------------------------ #
# GET /external-feeds (list)
# ------------------------------------------------------------------ #

class TestListEndpoint:

    def test_list_returns_series(self, monkeypatch):
        client = _build_test_client(monkeypatch, _make_user("user", "u-1"))
        # Upload one series
        client.post(
            "/api/forecast-ops/external-feeds",
            params={"series_key": "s1", "domain": "operating_rate",
                    "product_key": "p1"},
            files={"file": ("a.csv", b"date,value\n2025-01-01,1.0\n", "text/csv")},
        )

        resp = client.get("/api/forecast-ops/external-feeds")
        assert resp.status_code == 200
        rows = resp.json()
        assert isinstance(rows, list)
        assert any(r["series_key"] == "s1" for r in rows)

    def test_list_empty_when_no_series(self, monkeypatch):
        client = _build_test_client(monkeypatch, _make_user("user", "u-1"))
        resp = client.get("/api/forecast-ops/external-feeds")
        assert resp.status_code == 200
        assert resp.json() == []


# ------------------------------------------------------------------ #
# GET /external-feeds/{series_key}
# ------------------------------------------------------------------ #

class TestGetEndpoint:

    def test_get_returns_points(self, monkeypatch):
        client = _build_test_client(monkeypatch, _make_user("user", "u-1"))
        client.post(
            "/api/forecast-ops/external-feeds",
            params={"series_key": "s1", "domain": "operating_rate",
                    "product_key": "p1"},
            files={"file": ("a.csv", b"date,value\n2025-01-01,1.0\n2025-01-08,2.0\n",
                            "text/csv")},
        )
        resp = client.get("/api/forecast-ops/external-feeds/s1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["series_key"] == "s1"
        assert "points" in body
        assert len(body["points"]) == 2

    def test_get_unknown_returns_404(self, monkeypatch):
        client = _build_test_client(monkeypatch, _make_user("user", "u-1"))
        resp = client.get("/api/forecast-ops/external-feeds/nonexistent")
        assert resp.status_code == 404


# ------------------------------------------------------------------ #
# DELETE /external-feeds/{series_key} (admin-only)
# ------------------------------------------------------------------ #

class TestDeleteEndpoint:

    def test_admin_can_delete(self, monkeypatch):
        client = _build_test_client(monkeypatch, _make_user("admin", "a-1"))
        client.post(
            "/api/forecast-ops/external-feeds",
            params={"series_key": "s1", "domain": "operating_rate",
                    "product_key": "p1"},
            files={"file": ("a.csv", b"date,value\n2025-01-01,1.0\n",
                            "text/csv")},
        )

        resp = client.delete("/api/forecast-ops/external-feeds/s1")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_non_admin_403(self, monkeypatch):
        client = _build_test_client(monkeypatch, _make_user("user", "u-1"))
        # Upload as user
        client.post(
            "/api/forecast-ops/external-feeds",
            params={"series_key": "s1", "domain": "operating_rate",
                    "product_key": "p1"},
            files={"file": ("a.csv", b"date,value\n2025-01-01,1.0\n",
                            "text/csv")},
        )

        resp = client.delete("/api/forecast-ops/external-feeds/s1")
        assert resp.status_code == 403

    def test_delete_unknown_returns_404(self, monkeypatch):
        client = _build_test_client(monkeypatch, _make_user("admin", "a-1"))
        resp = client.delete("/api/forecast-ops/external-feeds/nonexistent")
        assert resp.status_code == 404