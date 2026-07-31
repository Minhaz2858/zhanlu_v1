# Dashboard E2E Suite

Validates the agent's ability to generate and manage a real-time dashboard on the
live MySQL: initialize, stable connection, live data streaming to the UI, and
graceful handling of connection interruptions — the full lifecycle.

## Run order (RAM-safe, explicit files only)

```bash
cd /root/zhanlu/backend
rm -f test_runtime.db
DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)"
DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest \
  tests/test_dashboard_e2e_helpers_unit.py \
  tests/test_dashboard_e2e_live.py \
  tests/test_dashboard_e2e_browser.py -v
```

## What each file covers

- `test_dashboard_e2e_helpers_unit.py` — pure helpers (psql row parsing, widget
  read-only guard). No network.
- `test_dashboard_e2e_live.py` — backend live E2E against the real Aliyun RDS
  MySQL: `create_dashboard` tool → persist → `run_dashboard_query`. Proves stable
  connection (NOW(6)+VERSION() rows), live streaming (two polls return different
  `ts`), and graceful connection-interruption (bad host → per-widget error,
  structure intact).
- `test_dashboard_e2e_browser.py` — browser E2E (playwright) on the deployed app:
  login → seed dashboard → My Space popup renders live data → value changes
  across a poll → bad-host dashboard shows a red per-widget error → a seeded chat
  message's dashboard card opens the right-side live popup.

## External dependencies (tests SKIP cleanly when absent)

- **Aliyun RDS MySQL** reachable. Override with `E2E_MYSQL_HOST/PORT/USER/PASS/DB`.
- **`docker exec zhanlu-postgres psql`** for config discovery (no committed secrets).
- **Deployed app** at `E2E_APP_URL` (default `http://localhost:8000`); login
  `E2E_ADMIN_EMAIL` / `E2E_ADMIN_PASS` (default `admin@zhanlu.dev` / `Test1234!`).
- **Playwright chromium** at `~/.cache/ms-playwright` (already installed).

## Design note

Live streaming is proven with time-varying table-free SQL (`SELECT NOW(6) AS ts`)
instead of mutating the production RDS — no write access required. The negative
fixture uses the unreachable `10.10.10.49` host for the connection-error path.
