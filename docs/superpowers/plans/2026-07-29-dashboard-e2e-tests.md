# Dashboard E2E Test Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An E2E suite proving the agent can generate and manage a real-time dashboard on the live MySQL — covering init, stable connection, live streaming to the UI, and graceful connection-interruption handling, end to end.

**Architecture:** Two layers. Backend pytest exercises `create_dashboard` tool → `run_dashboard_query` against the real Aliyun RDS MySQL using a scratch KB whose connection config is read from the live DB (no committed secrets). Browser pytest (playwright.sync_api) drives the deployed app on `localhost:8000`: login via API token, seed a dashboard via `POST /api/dashboards`, then verify the live popup, polling updates, and the error path. Both layers **skip (not fail)** when their external dependency (RDS / app / docker / chromium) is unavailable.

**Tech Stack:** pytest, playwright.sync_api (chromium already installed), pymysql (already installed), httpx (already installed), docker exec psql for config discovery.

## Global Constraints

- **RAM-limited box:** run only the explicit new test files by path; never the full suite; no `-n` workers.
- **No committed secrets:** MySQL config comes from `docker exec zhanlu-postgres psql` at run time, or `E2E_MYSQL_*` env vars for CI.
- **No RDS mutation:** live streaming is proven with time-varying table-free SQL (`SELECT NOW(6) AS ts`), not writes.
- **RDS skip guard:** tests `pytest.skip` with a clear reason when the RDS / live DB / app / chromium is unreachable.
- **Real MySQL target:** host `rm-bp1456yccem7e68zsmo.mysql.rds.aliyuncs.com:3306` (KB `ERP` / `test2`). Negative fixture: host `10.10.10.49:3306` (KB `aipdp_data_warehouse_prod`, id `90e80028-8e6e-4fdf-b91c-81c73aca9932`) — unreachable.
- **Test DB setup (sqlite):** `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)"`. sqlite enforces FKs — scratch KB rows must be real rows.
- **Login:** `POST http://localhost:8000/api/apps/default-app/auth/login` `{email, password}` → `{access_token, refresh_token}`. Frontend reads token from localStorage `base44_access_token`. Dev login: `admin@zhanlu.dev` / `Test1234!`.
- **Reuse (already built, do not re-implement):** `_create_dashboard(args, db, user_id, context)`, `run_dashboard_query(db, dashboard)`, `Dashboard`, `KnowledgeBase`.

---

## File Structure

**New:**
- `backend/tests/_dashboard_e2e_helpers.py` — shared helpers: RDS config discovery (`docker psql` + env override), `rds_reachable()` socket probe, `parse_psql_row()` pure parser, scratch-KB factory, NOW/VERSION widget dicts, admin-token + seed/cleanup helpers, chat-message psql seed.
- `backend/tests/test_dashboard_e2e_helpers_unit.py` — unit tests for the pure helpers (no docker/network).
- `backend/tests/test_dashboard_e2e_live.py` — backend live E2E (init, stable connection, polling, error isolation).
- `backend/tests/test_dashboard_e2e_browser.py` — browser E2E (login, My Space live+poll+error, chat popup).

**Modified:** none (the feature is already built; this is a characterization/E2E suite).

---

## Task 1: Shared E2E helpers + unit tests

**Files:**
- Create: `backend/tests/_dashboard_e2e_helpers.py`
- Test: `backend/tests/test_dashboard_e2e_helpers_unit.py`

**Interfaces:**
- Produces:
  - `parse_psql_row(line: str) -> dict | None` — parses one `id|host|port|db|user|pass` psql line.
  - `get_mysql_kb_config() -> dict | None` — env-override else docker-psql discovery; keys `kb_id, host, port, database_name, username, password`.
  - `rds_reachable(host, port=3306, timeout=4) -> bool`
  - `NOW_WIDGET`, `VERSION_WIDGET` dicts.
  - `make_scratch_kb(db, cfg, org_id, host_override=None) -> KnowledgeBase`
  - `require_live_or_skip() -> cfg` (pytest.skip when unavailable).

- [ ] **Step 1: Write the failing unit test**

```python
# backend/tests/test_dashboard_e2e_helpers_unit.py
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
from tests._dashboard_e2e_helpers import parse_psql_row, NOW_WIDGET, VERSION_WIDGET
from app.services.dashboard_query import validate_widget_sql


def test_parse_psql_row_valid():
    line = "bfab2d42-9985-4529-8a36-d6a66dbeb908|rm-x.mysql.rds.aliyuncs.com|3306|erpdb|erpuser|secret"
    cfg = parse_psql_row(line)
    assert cfg["kb_id"] == "bfab2d42-9985-4529-8a36-d6a66dbeb908"
    assert cfg["host"] == "rm-x.mysql.rds.aliyuncs.com"
    assert cfg["port"] == 3306
    assert cfg["database_name"] == "erpdb"
    assert cfg["username"] == "erpuser"
    assert cfg["password"] == "secret"


def test_parse_psql_row_bad():
    assert parse_psql_row("") is None
    assert parse_psql_row("only|three|cols") is None


def test_widgets_are_readonly_and_table_free():
    validate_widget_sql(NOW_WIDGET["sql"])      # no raise
    validate_widget_sql(VERSION_WIDGET["sql"])  # no raise
    assert "NOW(6)" in NOW_WIDGET["sql"]
    assert "VERSION()" in VERSION_WIDGET["sql"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_e2e_helpers_unit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests._dashboard_e2e_helpers'`

- [ ] **Step 3: Write the implementation**

```python
# backend/tests/_dashboard_e2e_helpers.py
"""Shared helpers for the dashboard E2E suite. Not a test module."""
from __future__ import annotations

import os
import socket
import subprocess

import pytest

RDS_HOST_MARK = "rds.aliyuncs.com"
BAD_HOST = "10.10.10.49"  # reachable nowhere from here -> fast "no route" fail

NOW_WIDGET = {"id": "w_ts", "type": "kpi", "title": "Live Time",
              "sql": "SELECT NOW(6) AS ts", "options": {"value_column": "ts"}}
VERSION_WIDGET = {"id": "w_ver", "type": "kpi", "title": "DB Version",
                  "sql": "SELECT VERSION() AS v", "options": {"value_column": "v"}}


def parse_psql_row(line: str) -> dict | None:
    parts = [p.strip() for p in (line or "").split("|")]
    if len(parts) < 6 or not parts[0]:
        return None
    try:
        port = int(parts[2] or 3306)
    except ValueError:
        return None
    return {"kb_id": parts[0], "host": parts[1], "port": port,
            "database_name": parts[3], "username": parts[4], "password": parts[5]}


def _docker_psql(sql: str) -> str | None:
    cmd = ["docker", "exec", "zhanlu-postgres", "psql", "-U", "zhanlu",
           "-d", "zhanlu", "-t", "-A", "-F", "|", "-c", sql]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def get_mysql_kb_config() -> dict | None:
    """Env override (CI) else docker-psql discovery. None if unavailable."""
    if os.environ.get("E2E_MYSQL_HOST"):
        return {"kb_id": None, "host": os.environ["E2E_MYSQL_HOST"],
                "port": int(os.environ.get("E2E_MYSQL_PORT", "3306")),
                "database_name": os.environ["E2E_MYSQL_DB"],
                "username": os.environ["E2E_MYSQL_USER"],
                "password": os.environ["E2E_MYSQL_PASS"]}
    sql = ("SELECT id, host, port, database_name, username, password "
           "FROM knowledge_bases WHERE source_kind='database' AND db_type='mysql' "
           "AND host LIKE '%rds.aliyuncs.com' AND is_deleted=false "
           "ORDER BY updated_date DESC LIMIT 1;")
    out = _docker_psql(sql)
    if not out:
        return None
    return parse_psql_row(out.splitlines()[0] if out else "")


def rds_reachable(host: str, port: int = 3306, timeout: int = 4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def make_scratch_kb(db, cfg, org_id, host_override=None):
    """Create a real KnowledgeBase row (in the test sqlite DB) pointing at the
    real MySQL. get_connector(kb) reads host/port/db/user/pass to connect."""
    from app.models.knowledge_base import KnowledgeBase
    kb = KnowledgeBase(
        name="e2e-scratch-mysql", source_kind="database", db_type="mysql",
        host=host_override or cfg["host"], port=cfg["port"],
        database_name=cfg["database_name"], username=cfg["username"],
        password=cfg["password"], org_id=org_id, app_id="default-app",
    )
    db.add(kb)
    db.commit()
    return kb


def require_live_or_skip():
    """Return the MySQL config or pytest.skip with a clear reason."""
    cfg = get_mysql_kb_config()
    if not cfg:
        pytest.skip("live DB config unavailable (docker/psql or E2E_MYSQL_* env)")
    if not rds_reachable(cfg["host"], cfg["port"]):
        pytest.skip(f"MySQL RDS unreachable: {cfg['host']}:{cfg['port']}")
    return cfg
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_e2e_helpers_unit.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/tests/_dashboard_e2e_helpers.py backend/tests/test_dashboard_e2e_helpers_unit.py
git commit -m "test(dashboards): shared E2E helpers + unit tests"
```

---

## Task 2: Backend live E2E (init, connection, polling, error)

**Files:**
- Create: `backend/tests/test_dashboard_e2e_live.py`

**Interfaces:**
- Consumes: helpers from Task 1; `_create_dashboard`, `run_dashboard_query`, `Dashboard`, `User`.

- [ ] **Step 1: Write the E2E test**

```python
# backend/tests/test_dashboard_e2e_live.py
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import asyncio
import time
import uuid
import pytest

from app.database import Base, engine, SessionLocal
from app.models.dashboard import Dashboard
from app.models.user import User
import app.models  # noqa: F401
from app.services.tool_handlers.dashboard_tools import _create_dashboard
from app.services.dashboard_query import run_dashboard_query
from tests._dashboard_e2e_helpers import (
    require_live_or_skip, make_scratch_kb, NOW_WIDGET, VERSION_WIDGET, BAD_HOST,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def user(db):
    u = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@t.io",
             full_name="t", role="user", password_hash="x",
             org_id="default-org", app_id="default-app")
    db.add(u)
    db.commit()
    return u


def _make_live_dashboard(db, user, cfg, host_override=None):
    kb = make_scratch_kb(db, cfg, org_id=user.org_id, host_override=host_override)
    res = _run(_create_dashboard(
        {"datasource_id": kb.id, "title": "E2E Live",
         "widgets": [NOW_WIDGET, VERSION_WIDGET], "refresh_interval_seconds": 10},
        db, user.id, context={"org_id": user.org_id}))
    assert res["success"] is True, res.get("error")
    return db.get(Dashboard, res["dashboard"]["id"])


def test_init_and_stable_connection(db, user):
    cfg = require_live_or_skip()
    dash = _make_live_dashboard(db, user, cfg)
    assert dash.datasource_kb_id  # persisted, bound to the live KB
    out = _run(run_dashboard_query(db, dash))
    ts = out["results"]["w_ts"]
    assert ts["error"] is None and ts["rows"], ts
    ver = out["results"]["w_ver"]
    assert ver["error"] is None and ver["rows"] and ver["rows"][0].get("v"), ver


def test_polling_streams_fresh_data(db, user):
    cfg = require_live_or_skip()
    dash = _make_live_dashboard(db, user, cfg)
    t1 = _run(run_dashboard_query(db, dash))["results"]["w_ts"]["rows"][0]["ts"]
    time.sleep(0.2)
    t2 = _run(run_dashboard_query(db, dash))["results"]["w_ts"]["rows"][0]["ts"]
    assert t1 != t2  # NOW(6) differs across polls => fresh live data each time


def test_connection_interruption_isolated_gracefully(db, user):
    cfg = require_live_or_skip()
    dash = _make_live_dashboard(db, user, cfg, host_override=BAD_HOST)
    out = _run(run_dashboard_query(db, dash))
    # Structure intact even though the host is unreachable
    assert out["dashboard_id"] == dash.id and "refreshed_at" in out
    ts = out["results"]["w_ts"]
    assert ts["error"], "expected a per-widget error for unreachable host"
    assert ts["rows"] == []
```

- [ ] **Step 2: Run the backend live E2E**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_e2e_live.py -v`
Expected: 3 PASS (or clean SKIP if the RDS is unreachable at run time — check the skip reason).

- [ ] **Step 3: Fix any failures**

- If `run_dashboard_query` errors on connect timeout: the mysql connector's connect attempt to `BAD_HOST` should fail fast ("No route to host"). If it hangs, set a short connect timeout by wrapping the bad-host probe — but prefer observing actual behavior first.
- If `w_ver` returns no `v`: some MySQL variants uppercase `VERSION()` output key — read `list(ver["rows"][0].values())[0]` instead of `["v"]` if needed.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_dashboard_e2e_live.py
git commit -m "test(dashboards): backend live E2E (init, connection, polling, error isolation)"
```

---

## Task 3: Browser E2E (login, My Space live+poll+error, chat popup)

**Files:**
- Create: `backend/tests/test_dashboard_e2e_browser.py`

**Interfaces:**
- Consumes: helpers from Task 1 (config discovery, reachability). Uses `httpx` for API calls, `playwright.sync_api` for the browser. Deployed app at `http://localhost:8000`.

- [ ] **Step 1: Write the browser E2E test**

```python
# backend/tests/test_dashboard_e2e_browser.py
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
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
                   timeout=10)
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
    return cfg


@pytest.fixture(scope="module")
def token(live):
    return _login_token()


def _open_browser_page():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch()
    return pw, browser


def test_myspace_live_data_and_polling(live, token):
    cfg = live
    kb_id = cfg["kb_id"]
    assert kb_id, "need the real ERP KB id from the live DB"
    did = _seed_dashboard(token, kb_id, "E2E Live")
    try:
        pw, browser = _open_browser_page()
        try:
            ctx = browser.new_context()
            page = ctx.new_page()
            page.add_init_script(f"localStorage.setItem('base44_access_token', '{token}');")
            page.goto(f"{APP}/my-space", wait_until="domcontentloaded")
            page.get_by_text("Dashboards", exact=True).click()
            page.get_by_text("E2E Live", exact=True).click()
            page.wait_for_selector("[data-testid='dashboard-popup']", timeout=10000)
            val1 = page.locator(".text-3xl").first.inner_text()
            page.wait_for_timeout(11000)  # > refresh_interval_seconds (10)
            val2 = page.locator(".text-3xl").first.inner_text()
            assert val1 and val1 != "—", "expected a live value"
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
            ctx = browser.new_context()
            page = ctx.new_page()
            page.add_init_script(f"localStorage.setItem('base44_access_token', '{token}');")
            page.goto(f"{APP}/my-space", wait_until="domcontentloaded")
            page.get_by_text("Dashboards", exact=True).click()
            page.get_by_text("E2E Broken", exact=True).click()
            page.wait_for_selector("[data-testid='dashboard-popup']", timeout=10000)
            # wait for the query to fail and render the red error widget
            page.wait_for_selector(".text-red-600", timeout=15000)
            body = page.locator("[data-testid='dashboard-popup']").inner_text()
            assert body, "popup should stay usable and show a per-widget error"
        finally:
            browser.close()
            pw.stop()
    finally:
        _delete_dashboard(token, did)
```

Chat-popup step (seeded via psql) is folded into the same file as an additional test that seeds a `chat_sessions` + `chat_messages` row whose `artifacts` references the dashboard, navigates to `/chat?session=<sid>`, asserts the dashboard card's **Open** button, clicks it, and asserts the chat-variant popup opens with a live value. If the chat-message seed proves brittle against the live DOM during execution, it is acceptable to rely on the component tests (`DashboardCard`, `DashboardPopup` chat variant) for the chat chrome and keep this browser file focused on the My Space live-data lifecycle — note the deviation in the commit message.

- [ ] **Step 2: Run the browser E2E**

Run: `cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_e2e_browser.py -v -s`
Expected: tests PASS (or clean SKIP with a clear reason). If selectors don't match the live DOM, adjust the text/selector to the actual rendered markup (inspect with a headed run or screenshot).

- [ ] **Step 3: Fix selectors / timing**

- If the KPI value selector (`.text-3xl`) matches more than the NOW widget, scope it to the widget titled "Live Time" (e.g. `page.locator("div:has-text('Live Time')").locator('.text-3xl')`).
- If 11s is flaky on a slow box, raise the wait or reduce `refresh_interval_seconds` to the 10s floor (already at floor).
- The error widget class is `text-red-600` (from `DashboardViewer`'s error div); confirm against the live DOM.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_dashboard_e2e_browser.py
git commit -m "test(dashboards): browser E2E (live data, polling, graceful error)"
```

---

## Task 4: Verification run + run-docs

**Files:**
- Create: `docs/superpowers/e2e/dashboard-e2e.md` (short run guide)

- [ ] **Step 1: Run the whole new suite together (RAM-safe: 4 small files)**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_dashboard_e2e_helpers_unit.py tests/test_dashboard_e2e_live.py tests/test_dashboard_e2e_browser.py -v`
Expected: all PASS, or clean SKIPs with clear reasons for any unavailable external dependency.

- [ ] **Step 2: Write the run guide**

```markdown
<!-- docs/superpowers/e2e/dashboard-e2e.md -->
# Dashboard E2E Suite

Run order (RAM-safe, explicit files only):

  cd /root/zhanlu/backend
  rm -f test_runtime.db
  DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)"
  DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest \
    tests/test_dashboard_e2e_helpers_unit.py \
    tests/test_dashboard_e2e_live.py \
    tests/test_dashboard_e2e_browser.py -v

External deps (tests SKIP cleanly when absent):
- Aliyun RDS MySQL reachable (else set E2E_MYSQL_HOST/PORT/USER/PASS/DB).
- `docker exec zhanlu-postgres psql` available for config discovery.
- Deployed app at E2E_APP_URL (default http://localhost:8000), login
  E2E_ADMIN_EMAIL/E2E_ADMIN_PASS (default admin@zhanlu.dev / Test1234!).
- Playwright chromium (installed at ~/.cache/ms-playwright).
```

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/e2e/dashboard-e2e.md
git commit -m "docs(dashboards): E2E suite run guide"
```

---

## Self-Review

**1. Spec coverage:**
- Initialize dashboard → Task 2 `test_init_and_stable_connection` + Task 3 seeding. ✓
- Stable DB connection → Task 2 (error:null, rows, version non-empty). ✓
- Fetch + stream live data → Task 2 `test_polling_streams_fresh_data` (ts differs) + Task 3 (popup value changes across a poll). ✓
- Connection interruption / sync errors → Task 2 `test_connection_interruption_isolated_gracefully` + Task 3 `test_connection_error_shows_gracefully`. ✓
- Full lifecycle → Task 2 (tool→persist→query) + Task 3 (seed→card/popup→live→poll→error). ✓
- Time-varying widget (no RDS mutation) → NOW_WIDGET/VERSION_WIDGET in Task 1. ✓
- Skip guards → `require_live_or_skip` + `_app_up` fixtures. ✓

**2. Placeholder scan:** no TBD/TODO. The chat-popup browser step is described with a concrete seeding approach and an explicitly-stated acceptable fallback (component tests) — not a placeholder. ✓

**3. Type consistency:**
- `get_mysql_kb_config()` keys `kb_id, host, port, database_name, username, password` — produced in Task 1, consumed identically in Task 2 (scratch KB) + Task 3 (`cfg["kb_id"]`, `cfg["host"]`). ✓
- `make_scratch_kb(db, cfg, org_id, host_override=None)` — Task 1 defines, Task 2 uses. ✓
- `NOW_WIDGET`/`VERSION_WIDGET` ids `w_ts`/`w_ver` — Task 2 reads `results["w_ts"]`/`results["w_ver"]`. ✓
- `require_live_or_skip()` returns cfg — Task 2 uses. ✓

No gaps. Plan ready for execution.
