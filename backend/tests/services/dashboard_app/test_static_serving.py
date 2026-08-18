"""Wave 0 T2 — verify the pre-built template frontend dist actually serves.

The template React bundle (backend/app/dashboards/_template/frontend/dist) is
copied into every generated app dir by the generator, then served by
DashboardAppManager at /api/dashboards/apps/{slug}/. This test proves that path
end-to-end with a real generated app + FastAPI TestClient, so a broken/missing
dist fails here instead of at the user's browser.
"""
import shutil
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.dashboard_app.generator import get_generator, TEMPLATE_DIR
from app.services.dashboard_app.manager import DashboardAppManager

SLUG = "_t2_serve_smoke"

SAMPLE_SPEC = {
    "name": "T2 Serving Smoke",
    "slug": SLUG,
    # description/design_system_ref intentionally absent → None in config:
    # regression guard for the tojson→NameError('null') bug.
    "datasource_id": "kb-t2",
    "metrics": [
        {"id": "kpi_x", "type": "kpi", "title": "X", "sql": "SELECT 1 AS v",
         "options": {"value_column": "v", "show_sparkline": True, "format": None}},
    ],
    "refresh_interval_seconds": 30,
    "theme": "light",
}


def _cleanup() -> None:
    """Unmount + drop cached modules + remove the generated app dir."""
    app_dir = get_generator().app_dir(SLUG)
    if app_dir.exists():
        shutil.rmtree(app_dir, ignore_errors=True)
    for name in ("api", "queries", "realtime"):
        sys.modules.pop(f"app.dashboards.{SLUG}.{name}", None)


def test_template_dist_exists_and_is_built() -> None:
    """Gate: the template frontend must have a built dist (index.html + bundle)."""
    dist = TEMPLATE_DIR / "frontend" / "dist"
    assert dist.exists(), "template frontend/dist missing — run the vite build"
    index = dist / "index.html"
    assert index.is_file(), "template dist/index.html missing"
    assets = list((dist / "assets").glob("index-*.js"))
    assert assets, "template dist has no JS bundle in assets/"
    # Relative base so the app works under /api/dashboards/apps/{slug}/...
    html = index.read_text(encoding="utf-8")
    assert './assets/' in html or '"./assets/' in html


def test_generated_app_serves_index_and_assets() -> None:
    _cleanup()
    manager = DashboardAppManager()
    app = FastAPI()
    manager.init_app(app)
    try:
        get_generator().generate(SAMPLE_SPEC)
        manager.mount(SLUG)
        client = TestClient(app)

        # SPA entry
        r = client.get(f"/api/dashboards/apps/{SLUG}/")
        assert r.status_code == 200
        assert '<div id="root">' in r.text or "root" in r.text

        # JS bundle asset
        assets = list((get_generator().app_dir(SLUG) / "dist" / "assets").glob("index-*.js"))
        assert assets
        r2 = client.get(f"/api/dashboards/apps/{SLUG}/assets/{assets[0].name}")
        assert r2.status_code == 200
        assert r2.headers.get("content-type", "").startswith("text/javascript")

        # app config endpoint from the generated api.py router
        r3 = client.get(f"/api/dashboards/apps/{SLUG}/config")
        assert r3.status_code == 200
        assert r3.json()["slug"] == SLUG
    finally:
        try:
            manager.unmount(SLUG)
        finally:
            _cleanup()
