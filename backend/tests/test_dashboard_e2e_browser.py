import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import json
import subprocess
import uuid
import pytest
import httpx

from tests._dashboard_e2e_helpers import (
    get_mysql_kb_config, rds_reachable, NOW_WIDGET, VERSION_WIDGET,
)

APP = os.environ.get("E2E_APP_URL", "http://localhost:8000")
ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", "admin@zhanlu.dev")
ADMIN_PASS = os.environ.get("E2E_ADMIN_PASS", "Test1234!")
BAD_KB_ID = "90e80028-8e6e-4fdf-b91c-81c73aca9932"  # aipdp_data_warehouse_prod (unreachable)

LIVE_VALUE_SEL = "div.rounded-xl:has-text('Live Time') .text-3xl"


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


def _seed_dashboard(token, kb_id, name):
    r = httpx.post(f"{APP}/api/dashboards",
                   headers={"Authorization": f"Bearer {token}"},
                   json={"name": name, "datasource_kb_id": kb_id,
                         "definition": {"widgets": [NOW_WIDGET, VERSION_WIDGET]},
                         "refresh_interval_seconds": 10},
                   timeout=15)
    r.raise_for_status()
    return r.json()["id"]


def _delete_dashboard(token, did):
    try:
        httpx.delete(f"{APP}/api/dashboards/{did}",
                     headers={"Authorization": f"Bearer {token}"}, timeout=10)
    except Exception:
        pass


@pytest.fixture(scope="module")
def live():
    cfg = get_mysql_kb_config()
    if not cfg or not rds_reachable(cfg["host"], cfg["port"]):
        pytest.skip("RDS unreachable")
    if not _app_up():
        pytest.skip(f"app unreachable at {APP}")
    if not cfg.get("kb_id"):
        pytest.skip("real ERP KB id unavailable from live DB")
    return cfg


@pytest.fixture(scope="module")
def token(live):
    return _login_token()


def _open_browser_page():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch()
    return pw, browser


def _open_authed_dashboard(page, token, dashboard_name):
    page.add_init_script(f"localStorage.setItem('base44_access_token', '{token}');")
    page.goto(f"{APP}/my-space", wait_until="domcontentloaded")
    page.get_by_text("Dashboards", exact=True).click()
    page.get_by_text(dashboard_name, exact=True).click()
    page.wait_for_selector("[data-testid='dashboard-popup']", timeout=10000)


def _wait_live_value(page, timeout_ms=20000):
    """Return the current NOW() KPI value once it's no longer the placeholder."""
    page.wait_for_selector(LIVE_VALUE_SEL, timeout=timeout_ms)
    for _ in range(int(timeout_ms / 500)):
        txt = page.locator(LIVE_VALUE_SEL).first.inner_text().strip()
        if txt and txt != "—":
            return txt
        page.wait_for_timeout(500)
    return page.locator(LIVE_VALUE_SEL).first.inner_text().strip()


def test_myspace_live_data_and_polling(live, token):
    did = _seed_dashboard(token, live["kb_id"], "E2E Live")
    try:
        pw, browser = _open_browser_page()
        try:
            page = browser.new_context().new_page()
            _open_authed_dashboard(page, token, "E2E Live")
            val1 = _wait_live_value(page)
            assert val1 and val1 != "—", "expected a live value"
            page.wait_for_timeout(11000)  # > refresh_interval_seconds (10)
            val2 = _wait_live_value(page)
            assert val1 != val2, "expected the live value to change across a poll"
        finally:
            browser.close()
            pw.stop()
    finally:
        _delete_dashboard(token, did)


def test_connection_error_shows_gracefully(live, token):
    did = _seed_dashboard(token, BAD_KB_ID, "E2E Broken")
    try:
        pw, browser = _open_browser_page()
        try:
            page = browser.new_context().new_page()
            _open_authed_dashboard(page, token, "E2E Broken")
            # the unreachable host fails the query -> red per-widget error renders
            page.wait_for_selector(".text-red-600", timeout=20000)
            body = page.locator("[data-testid='dashboard-popup']").inner_text()
            assert body, "popup should stay usable and show a per-widget error"
        finally:
            browser.close()
            pw.stop()
    finally:
        _delete_dashboard(token, did)


# --- chat inline popup path (seeded message) ---------------------------------

def _psql(sql):
    cmd = ["docker", "exec", "zhanlu-postgres", "psql", "-U", "zhanlu",
           "-d", "zhanlu", "-t", "-A", "-c", sql]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _seed_chat_message(did, title):
    uid = _psql(f"SELECT id FROM users WHERE email='{ADMIN_EMAIL}' LIMIT 1;")
    if not uid:
        return None, None
    sid = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    artifacts = json.dumps([{
        "source": "dashboard", "dashboard_id": did, "title": title,
        "datasource_name": "ERP", "widget_count": 2,
    }]).replace("'", "''")
    _psql(
        "INSERT INTO chat_sessions (id, title, org_id, app_id, created_by_id, "
        "created_date, updated_date, is_deleted) VALUES "
        f"('{sid}', 'E2E Chat', 'default-org', 'default-app', '{uid}', now(), now(), false);"
    )
    _psql(
        "INSERT INTO chat_messages (id, session_id, role, content, org_id, app_id, "
        "created_by_id, created_date, updated_date, is_deleted, artifacts) VALUES "
        f"('{mid}', '{sid}', 'assistant', 'Here is your live dashboard.', "
        f"'default-org', 'default-app', '{uid}', now(), now(), false, '{artifacts}'::json);"
    )
    return sid, mid


def _delete_chat(sid):
    if sid:
        _psql(f"DELETE FROM chat_messages WHERE session_id='{sid}';")
        _psql(f"DELETE FROM chat_sessions WHERE id='{sid}';")


def test_chat_inline_dashboard_card_and_popup(live, token):
    """A chat message carrying the dashboard artifact renders a card that opens
    the right-side live popup (the inline-chat requirement)."""
    did = _seed_dashboard(token, live["kb_id"], "E2E Live")
    sid, _mid = _seed_chat_message(did, "E2E Live")
    if not sid:
        _delete_dashboard(token, did)
        pytest.skip("could not seed chat session (admin user lookup failed)")
    try:
        pw, browser = _open_browser_page()
        try:
            page = browser.new_context().new_page()
            page.add_init_script(f"localStorage.setItem('base44_access_token', '{token}');")
            page.goto(f"{APP}/chat?session={sid}", wait_until="domcontentloaded")
            # the dashboard card renders an Open button for the seeded artifact
            open_btn = page.get_by_role("button", name="Open")
            open_btn.wait_for(timeout=15000)
            open_btn.click()
            page.wait_for_selector("[data-testid='dashboard-popup']", timeout=10000)
            val = _wait_live_value(page)
            assert val and val != "—", "expected a live value in the chat popup"
        finally:
            browser.close()
            pw.stop()
    finally:
        _delete_chat(sid)
        _delete_dashboard(token, did)
