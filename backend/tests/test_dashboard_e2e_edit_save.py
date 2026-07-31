"""E2E: creator edits a dashboard widget (title + SQL), previews, saves, reloads.

Per-file run only (user rejects combined-suite runs). Skips if the live app /
RDS / a real MySQL KB id are unavailable. Real Aliyun RDS MySQL — read-only
SELECTs only (NO writes).

Requires the deployed app to have the Phase 3 routes (PATCH /{id} +
POST /{id}/preview-sql) — i.e. run AFTER merging dashboard-phase3 + deploying.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import pytest
import httpx

from tests._dashboard_e2e_helpers import get_mysql_kb_config, rds_reachable, NOW_WIDGET, VERSION_WIDGET

APP = os.environ.get("E2E_APP_URL", "http://localhost:8000")
ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", "admin@zhanlu.dev")
ADMIN_PASS = os.environ.get("E2E_ADMIN_PASS", "Test1234!")


def _app_up() -> bool:
    try:
        return httpx.get(f"{APP}/api/dashboards", timeout=4).status_code in (401, 403)
    except Exception:
        return False


def _login_token() -> str:
    r = httpx.post(f"{APP}/api/apps/default-app/auth/login",
                   json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=10)
    r.raise_for_status()
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def cfg():
    c = get_mysql_kb_config()
    if not c or not c.get("kb_id"):
        pytest.skip("live MySQL KB id unavailable (docker/psql or E2E_MYSQL_* env)")
    if not rds_reachable(c["host"], c["port"]):
        pytest.skip(f"MySQL RDS unreachable: {c['host']}:{c['port']}")
    if not _app_up():
        pytest.skip(f"app not up at {APP}")
    return c


def test_creator_edits_previews_saves(cfg):
    token = _login_token()
    h = {"Authorization": f"Bearer {token}"}
    # seed a dashboard owned by the logged-in admin (creator)
    seed = httpx.post(f"{APP}/api/dashboards", headers=h, json={
        "name": "E2E Edit", "datasource_kb_id": cfg["kb_id"],
        "definition": {"widgets": [NOW_WIDGET]},
        "refresh_interval_seconds": 10,
    }, timeout=15)
    seed.raise_for_status()
    did = seed.json()["id"]
    try:
        # PATCH: rename + replace the widget with a read-only SQL (DB version)
        new_sql = "SELECT VERSION() AS v"
        patch = httpx.patch(f"{APP}/api/dashboards/{did}", headers=h, json={
            "name": "E2E Edited",
            "definition": {"widgets": [
                {"id": "w1", "type": "table", "title": "Version", "sql": new_sql, "options": {}}]}})
        assert patch.status_code == 200, patch.text
        assert patch.json()["name"] == "E2E Edited"
        assert patch.json()["can_edit"] is True  # creator flag

        # preview the new SQL (creator-only) — returns a row from VERSION()
        prev = httpx.post(f"{APP}/api/dashboards/{did}/preview-sql", headers=h,
                          json={"sql": new_sql}, timeout=15)
        assert prev.status_code == 200, prev.text
        assert prev.json()["error"] is None
        assert prev.json()["rows"]  # VERSION() returns one row

        # reload confirms persistence
        got = httpx.get(f"{APP}/api/dashboards/{did}", headers=h, timeout=10)
        assert got.status_code == 200
        assert got.json()["name"] == "E2E Edited"
        assert got.json()["definition"]["widgets"][0]["sql"] == new_sql
    finally:
        httpx.delete(f"{APP}/api/dashboards/{did}", headers=h, timeout=10)
