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


def test_generated_app_serves_index_and_assets(monkeypatch) -> None:
    _cleanup()
    # T17: apps are served by the GLOBAL catch-all routes in routers/dashboards.py
    # (per-app mounts were removed). Include that router and point its module-level
    # manager at a lightweight fake (get_app/resolve_app_dir) so the test never
    # touches the real Postgres registry.
    from app.routers import dashboards as dash_router

    class _FakeManager:
        def get_app(self, slug):  # noqa: ARG002
            return {"slug": slug}  # truthy → app record exists

        def resolve_app_dir(self, slug):
            return get_generator().app_dir(slug)

    app = FastAPI()
    app.include_router(dash_router.router, prefix="/api")
    monkeypatch.setattr(dash_router, "dashboard_app_manager", _FakeManager())
    try:
        get_generator().generate(SAMPLE_SPEC)
        client = TestClient(app)

        # SPA entry
        r = client.get(f"/api/dashboards/apps/{SLUG}/")
        assert r.status_code == 200
        assert '<div id="root">' in r.text or "root" in r.text
        # Cache policy: index.html must revalidate so redeploys reach the
        # browser (new hashed bundle reference); never heuristically cached.
        assert r.headers.get("cache-control", "").startswith("no-cache")

        # JS bundle asset
        assets = list((get_generator().app_dir(SLUG) / "dist" / "assets").glob("index-*.js"))
        assert assets
        r2 = client.get(f"/api/dashboards/apps/{SLUG}/assets/{assets[0].name}")
        assert r2.status_code == 200
        assert r2.headers.get("content-type", "").split(";")[0] in (
            "application/javascript", "text/javascript",
        )
        # Hashed assets are content-addressed → safe to cache long-term.
        assert "immutable" in r2.headers.get("cache-control", "")

        # app config: T17 serves the generated config.json from the app root
        # (the generated api.py router is no longer mounted, so /config.json is
        # the canonical endpoint — the frontend fetches it at startup).
        r3 = client.get(f"/api/dashboards/apps/{SLUG}/config.json")
        assert r3.status_code == 200
        assert r3.json()["slug"] == SLUG
        # config.json is regenerated on rebuild → must revalidate too.
        assert r3.headers.get("cache-control", "").startswith("no-cache")
    finally:
        _cleanup()
