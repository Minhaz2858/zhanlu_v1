# Dashboard E2E Test Suite — Design Spec

- **Date:** 2026-07-29
- **Status:** Draft (pending review)
- **Author:** brainstorming session (superpowers)
- **Feature under test:** Dynamic Live Dashboards (spec `2026-07-29-dynamic-dashboards-design.md`)

## 1. Context & Goal

The live-dashboard feature is built and deployed (11 commits on master). This spec
defines an **end-to-end test suite** proving an agent can generate and manage a
real-time dashboard against a **live database**, covering the full lifecycle:
agent invocation → stable DB connection → live data streaming to the UI →
graceful handling of connection interruptions / sync errors.

### Decisions (from brainstorming Q&A)
| Question | Decision |
|---|---|
| Test layers | **Backend API + Browser UI** |
| Agent step | **Invoke `create_dashboard` tool directly** (deterministic) |
| Live database | **Real project MySQL** (Aliyun RDS) |

### Environment facts (verified 2026-07-29)
- Real MySQL datasources (live postgres `knowledge_bases`): `ERP` (id
  `bfab2d42-9985-4529-8a36-d6a66dbeb908`) and `test2`, both
  `db_type=mysql`, host `rm-bp1456yccem7e68zsmo.mysql.rds.aliyuncs.com:3306`.
  **RDS is REACHABLE** from the dev box.
- `demo_warehouse_prod` (id `90e80028-...`), mysql host `10.10.10.49:3306`
  — **UNREACHABLE** (ideal negative fixture for the connection-interruption path).
- Internal `Customer Database` (postgres `zhanlu-postgres:5432`) is NOT exposed
  to localhost.
- Live postgres is reachable **only via `docker exec zhanlu-postgres psql`**
  (docker-bridge IP `172.22.0.4` is not routable from the host shell; direct
  psycopg2 fails "No route to host").
- `pymysql 2.2.8`, `playwright-py`, `psycopg2` all installed.
- Deployed app answers on `http://localhost:8000` (`/api/dashboards` → 401).
- Test login: `admin@zhanlu.dev` / `Test1234!`.
- **RAM-limited box:** explicit single test files only; never the full suite; no `-n`.

## 2. Key robustness choice — time-varying widgets (no RDS mutation)

Validating "live data updates stream to the UI" by mutating a production RDS is
unsafe and depends on unknown write/DDL rights. Instead the suite proves real-time
streaming with **time-varying, table-free widget SQL**:

- `SELECT NOW(6) AS ts` — different value every poll. Two consecutive `/query`
  polls returning different `ts` **proves** the dashboard re-queries and streams
  fresh data. No write access, no mutation.
- `SELECT VERSION() AS v` (or `DATABASE()`) — stable value; proves connectivity
  and correct data without needing a known table name.

This keeps the suite safe on the live RDS while still proving real-time behavior.

## 3. Architecture — two layers

### 3.1 Backend E2E (pytest)
`backend/tests/test_dashboard_e2e_live.py`

Exercises the real path against the real MySQL:
1. **Config discovery:** read the ERP KB's connection config from the live DB via
   `docker exec zhanlu-postgres psql` subprocess (`SELECT host, port,
   database_name, username, password FROM knowledge_bases WHERE db_type='mysql'
   AND host LIKE '%rds.aliyuncs.com'`). No committed secrets.
2. **Scratch KB:** create a scratch `KnowledgeBase` row in the **isolated test
   sqlite DB** (`test_runtime.db`) with the real RDS config
   (`source_kind='database'`, `db_type='mysql'`). App records stay isolated;
   live queries go to the real MySQL via `get_connector(kb)`.
3. **Lifecycle:** `create_dashboard` tool → `Dashboard` persisted →
   `run_dashboard_query(db, dashboard)` → live rows from the RDS.
4. **Guard:** `pytest.skip` (not fail) with a clear reason if `docker`/psql or the
   RDS is unreachable, so the suite is green off-network and full on-network.

### 3.2 Browser E2E (pytest + playwright.sync_api)
`backend/tests/test_dashboard_e2e_browser.py`

Drives the deployed app (`http://localhost:8000`) in a headless browser:
1. **Login** as `admin@zhanlu.dev` (UI form or API → token).
2. **Seed** a dashboard bound to the real ERP KB via `POST /api/dashboards`
   (admin Bearer token) with `NOW(6)` + `VERSION()` widgets — the deterministic
   equivalent of the agent's `create_dashboard`. No LLM calls.
3. **Open** the project chat, assert the dashboard **card** renders.
4. **Click** the card → right-side **popup** opens; assert live data is displayed.
5. **Poll:** wait > `refresh_interval_seconds`, assert the displayed value
   **changed** (live streaming).
6. **Error path:** open a dashboard bound to the unreachable host (`10.10.10.49`);
   assert a red per-widget error is shown and the page stays usable.
7. **Cleanup:** `DELETE /api/dashboards/{id}` for seeded dashboards.
8. **Guard:** skip if the app or RDS is unreachable, or if no chromium binary is
   available (`playwright install chromium` is a documented one-time setup step).

## 4. Scenarios → requirement mapping

| Requirement | Backend | Browser |
|---|---|---|
| Initialize dashboard | `create_dashboard` returns success + artifact; `Dashboard` row persisted bound to ERP KB | card appears in chat after seeding |
| Stable DB connection | first `/query` poll `error:null`, rows returned; connector honors row cap + timeout | popup shows live value, no error banner |
| Fetch + stream live data | poll #1 `ts` ≠ poll #2 `ts`; `VERSION()` stable + non-empty | popup value visibly changes across a poll |
| Connection interruption / sync errors | bad-host KB → per-widget `error` string, HTTP 200, structure intact (not blank/500) | red error widget rendered, page usable |
| Full lifecycle | tool → persist → `/query` (live) | seed → chat card → popup → live data → poll update → error UI |

## 5. RAM-safety & determinism
- Backend: explicit single test files; no full-suite; no `-n` workers.
- Browser: one spec; headless; no server startup (reuses the deployed app); no LLM.
- Both layers skip (not fail) when their external dependency (RDS / app / docker /
  chromium) is unavailable, keeping the suite green in restricted environments.

## 6. Security
- No credentials committed. MySQL config is read from the live DB at run time via
  `docker exec … psql`, or supplied via env vars (`E2E_MYSQL_HOST/PORT/USER/PASS/DB`)
  for CI. The login password stays in the existing dev-only convention, never
  written to source.
- Seeded dashboards are always deleted in a `finally`/teardown.

## 7. Open questions (for the plan)
1. Playwright chromium binary present? If not, the plan adds a one-time
   `python3 -m playwright install chromium` setup step (or skips the browser test).
2. Browser login path: UI form vs API token injection (pick whichever the deployed
   app's auth flow supports cleanly; verify in the plan).
3. Whether to target the `ERP` KB or `test2` KB (same RDS) — pick whichever has
   the most permissive/readable default schema; default to `ERP`.

## 8. Testing the suite itself
- The suite is validated by running it: backend file green (or cleanly skipped),
  browser file green (or cleanly skipped), on the deployed server.
