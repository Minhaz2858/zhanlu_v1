# Database Diagnostic Guide

> **"Which database is the backend actually talking to right now?"**

This page is the answer. It exists because the project has been bitten by
"silent SQLite fallback" once already: a leftover empty `zhanlu.db` file in
the repo root looked like the real database, while the actual data was in
PostgreSQL inside the Docker stack. To prevent this from recurring, the
backend now **refuses to start** if `DATABASE_URL` is missing or points at
an unknown driver, and there are three one-command ways to check which
engine is in use.

---

## 1. Startup log line

Every time the backend container starts, the **first** non-prestart line in
the log is:

```
[zhanlu] Connected to postgresql+psycopg2://zhanlu:***@postgres:5432/zhanlu (org_id=default-org, app_id=default-app)
```

(Also emitted as a `WARNING` from logger `zhanlu.startup.db`.) If this
line does not match the database you expect, stop and fix `.env`.

## 2. `GET /api/_db-info` (live, no auth)

```bash
curl -s http://localhost:5002/api/_db-info | jq .
```

Returns a JSON snapshot of:

- `engine.{driver,scheme,user,host,port,database}` — parsed SQLAlchemy URL.
- `database_url` — the same URL with the password masked.
- `is_sqlite` — convenience boolean.
- `server_version` — what the DB itself reports (e.g. `PostgreSQL 16.14`).
- `alembic_version` — the current schema version, or `null` if missing.
- `table_counts` — row count for every entity table present in the DB.

This endpoint is intentionally unauthenticated (it leaks no PII) so you
can hit it from curl, the browser console, or an ops dashboard without a
token.

## 3. `make db-info`

Wrapper that runs both of the above in one command. Use this whenever
something looks "off" with data.

```
$ make db-info
=== /api/_db-info (live) ===
{ ... full JSON ... }

=== check_db.py (run inside backend container) ===
────────────────────────────────────────
  ACTIVE DATABASE
────────────────────────────────────────
  URL     : postgresql+psycopg2://zhanlu:***@postgres:5432/zhanlu
  Driver  : postgresql+psycopg2
  Host    : postgres:5432
  Database: zhanlu
  User    : zhanlu
  ...
```

## 4. `backend/scripts/check_db.py`

Standalone Python diagnostic (no server needed). Useful for CI checks and
for debugging on a machine that doesn't have the backend running.

```bash
# From the host (reads backend/.env automatically when run from backend/):
cd backend
PYTHONPATH=. ./venv/bin/python scripts/check_db.py

# Or inside the running container:
docker exec -e PYTHONPATH=/app zhanlu-backend python /app/scripts/check_db.py
```

Exit codes:

- `0` — connected, summary printed
- `1` — `DATABASE_URL` missing/invalid (config refused to load)
- `2` — connection failed (wrong host, bad creds, network down)
- `3` — `alembic_version` table missing (schema not migrated)

---

## What changed (and why)

`backend/app/config.py` used to default `DATABASE_URL` to
`"sqlite:///./zhanlu.db"`. This is now removed. If the env var is missing
or empty, the config layer raises with a message that lists valid
options:

```
Value error, DATABASE_URL is required but is not set.
Set it in backend/.env or your environment, e.g.
  DATABASE_URL=postgresql+psycopg2://zhanlu:zhanlu123@postgres:5432/zhanlu
  DATABASE_URL=sqlite:///./zhanlu.db
See backend/.env.example for the full list of options.
```

An unknown driver prefix (`mongodb://…`, `redis://…`, etc.) also fails
loudly:

```
Value error, DATABASE_URL must start with a known driver prefix
(sqlite://, postgresql+psycopg2://, mysql+pymysql://, ...).
Got: 'mongodb://localhost/test'
```

The `is_sqlite` property and the SQLite-specific startup code paths in
`main.py` are kept intact, so opting into SQLite explicitly
(`DATABASE_URL=sqlite:///./zhanlu.db`) still works for offline single-user
dev — you just have to mean it.
