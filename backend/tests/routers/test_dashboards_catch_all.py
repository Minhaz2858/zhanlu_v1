"""T17 regression: global catch-all routes for generated dashboard apps.

Replaces the per-app `app.mount(StaticFiles)` that used to serve a dashboard
app's `dist/` files. The catch-all route resolves the on-disk app dir per
request so it works on any worker, for any app, without a restart.

Covers: 200 on a static file (config.json), correct MIME types for .js/.css/
.json/.html, SPA fallback to index.html for client routes, `..` traversal
rejected (400), and a missing slug → 404 JSON.
"""

import os
import sys
import uuid
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
import app.models  # noqa: F401  register all models
from app.models.dashboard_app import DashboardApp
from app.routers.dashboards import router as dashboards_router


# Use an isolated in-memory SQLite engine for this test module (non-shared so
# each session via the sessionmaker sees the same single connection).
_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(_engine)
_TestSession = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture
def db():
    Base.metadata.drop_all(_engine)
    Base.metadata.create_all(_engine)
    s = _TestSession()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client(db, tmp_path, monkeypatch):
    # Create the DashboardApp row the route checks for existence.
    slug = "erp-sales-overview"
    app_dir = tmp_path / slug
    dist = app_dir / "dist"
    dist.mkdir(parents=True)
    (dist / "config.json").write_text('{"metrics": []}')
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "app.js").write_text("console.log(1)")
    (dist / "style.css").write_text("body{}")

    rec = DashboardApp(
        id=str(uuid.uuid4()), slug=slug, name="ERP Sales Overview",
        status="running", org_id="default-org", app_id="default-app",
        datasource_kb_id="kb1", spec={},
    )
    db.add(rec)
    db.commit()

    # config.json is generated at the app ROOT (not in dist/).
    (app_dir / "config.json").write_text('{"metrics": []}')

    # Make resolve_app_dir return our tmp dir instead of the real generator dir.
    def _fake_resolve(self, s):
        return app_dir if s == slug else None
    monkeypatch.setattr(
        "app.services.dashboard_app.manager.DashboardAppManager.resolve_app_dir",
        _fake_resolve,
    )
    # get_app() reads the global SessionLocal; stub it to return our test row.
    monkeypatch.setattr(
        "app.services.dashboard_app.manager.DashboardAppManager.get_app",
        lambda self, s: rec if s == slug else None,
    )

    app = FastAPI()
    app.include_router(dashboards_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_config_json_200(client):
    r = client.get("/api/dashboards/apps/erp-sales-overview/config.json")
    assert r.status_code == 200, r.text
    assert r.json() == {"metrics": []}


def test_mime_types(client):
    js = client.get("/api/dashboards/apps/erp-sales-overview/app.js")
    css = client.get("/api/dashboards/apps/erp-sales-overview/style.css")
    html = client.get("/api/dashboards/apps/erp-sales-overview/index.html")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert html.status_code == 200
    assert "text/html" in html.headers["content-type"]


def test_spa_fallback_to_index(client):
    # A client-side route with no file extension → index.html.
    r = client.get("/api/dashboards/apps/erp-sales-overview/some/client/route")
    assert r.status_code == 200
    assert "<html>app</html>" in r.text
    assert "text/html" in r.headers["content-type"]


def test_traversal_rejected(client):
    # URL-encode the `..` so the client does not collapse it before routing;
    # the resolver must reject the escaped path with 400.
    r = client.get("/api/dashboards/apps/erp-sales-overview/%2e%2e%2f%2e%2e%2fetc%2fpasswd")
    assert r.status_code == 400
    assert r.json()["detail"] == "Invalid path"


def test_missing_slug_404(client):
    r = client.get("/api/dashboards/apps/does-not-exist/config.json")
    assert r.status_code == 404
    assert r.json()["detail"] == "Dashboard app not found"


def test_missing_file_404(client):
    r = client.get("/api/dashboards/apps/erp-sales-overview/missing.xyz")
    assert r.status_code == 404
    assert r.json()["detail"] == "Not found"
