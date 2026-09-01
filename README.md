# Zhanlu — Enterprise AI OS

Zhanlu is an enterprise AI operating system built by SYNEXIA. Users chat with an
autonomous main agent that plans, delegates to subagents, connects to business
databases, and produces polished artifacts (PPT, DOCX, dashboards, HTML) through
a governed, audited runtime — all inside a single web workspace.

- **Frontend:** React (Vite) + Tailwind — chat, agent studio, capability picker,
  live execution timeline, inline artifact preview.
- **Backend:** FastAPI + PostgreSQL + Redis + MinIO + Docker sandbox execution.

## Architecture

Zhanlu is organized in seven layers (see `docs/07_existing_architecture/`):

```
Layer 1 — Enterprise Interaction & Identity Layer
  User channels, identity, app/workspace selection, inline artifact preview

Layer 2 — Synexia Cognitive Core
  Goal, context, planning, reasoning, decision, reflection, learning
  (creates TaskSpec, ContextManifest, PlanDAG, PolicyDecision)

Layer 3 — Enterprise Harness Agent, Skill & Data Runtime
  Harness agent profiles, agent/datasource/skill bindings, Tool/Skill Gateway,
  governed NL2SQL, skill discovery + factory

Layer 4 — Enterprise Memory & Knowledge Layer
  Memory, knowledge graph, semantic model, metric definitions, experience library

Layer 5 — Enterprise Execution Layer
  Workflow engine, automation engine, sandbox runtime, artifact generation

Layer 6 — Enterprise Platform Services
  Security, observability, governance, cost, model management

Layer 7 — Infrastructure Layer
  Docker, PostgreSQL, Redis, MinIO, network, compute
```

Core design principles:

- **Synexia is the only orchestration brain.** Main agents may delegate to
  subagents under its control; users select high-level **capabilities** (Make
  PPT, Database Analysis, Make Dashboard, Make DOCX, Scheduled Reports), not raw
  internal skills (raw skills are available in Advanced Settings for
  developers/admins only).
- **Governed data access.** Users connect databases once in My Space / Databases
  & KB. Each agent uses only the datasources explicitly bound to it; subagents
  inherit none by default. All DB access goes through the Datasource Gateway and
  produces DataSnapshots. **The sandbox never receives raw database
  credentials.**
- **Artifacts, not attachments.** Generated files are versioned Artifacts with
  permission-checked inline preview (MD / HTML / PPTX / DOCX / dashboard /
  mini-app).
- **PostgreSQL is the source of truth.** Redis is temporary queue/cache/locks/
  events only. Sandbox filesystems are ephemeral and destroyed after execution.

## Repository layout

```
backend/          FastAPI app: routers, services, models, skills, sandbox_worker
  app/routers/    HTTP API (auth, apps, agents, conversations, artifacts, ...)
  app/services/   auth, agent FSM, dashboard pipeline, tool handlers, ...
  app/models/     SQLAlchemy models
  alembic/        database migrations
  sandbox_worker/ Redis-queue worker that spawns disposable Docker sandboxes
  skills/         built-in skills (PPT, DOCX, dashboard, data analysis, ...)
  tests/          pytest suite
frontend/         React (Vite) app: chat UI, agent studio, preview cards
docs/             architecture specs, API contract, database schema, plans
deploy/ infra/    deployment and infrastructure assets
docker-compose.yml            full stack: backend, worker, postgres, redis, minio, sandboxes
docker-compose.override.yml   local development overrides
.env.example                  environment template (root)
```

## Quick start (Docker)

```bash
cp .env.example .env            # then fill in secrets (JWT_SECRET, DB passwords, LLM key)
docker compose up -d --build
```

Services: `zhanlu-backend` (API + agent FSM + dashboard pipeline),
`zhanlu-sandbox-worker` (BRPOPs the Redis `sandbox:queue`, spawns disposable
sandbox containers for skill execution), PostgreSQL 16, Redis 7, MinIO (artifact
blob storage), and sandbox images (python / office / pptx / webapp).

Frontend development runs separately with Vite (see `frontend/README.md`).

## Authentication

Login is **required for every page and every mutating/LLM/data endpoint** — there
is no anonymous access.

- **Methods:** email + password. Registration verifies via an emailed OTP code
  (unless no users exist yet, for first-time setup).
- **Tokens:** a short-lived access JWT (default 15 min, carries a `jti`) plus a
  long-lived refresh token (default 30 days, rotated on each refresh, stored as
  a SHA-256 hash in `refresh_tokens`). On logout the access token's JTI is
  blacklisted (`revoked_tokens`) and all of the user's refresh tokens are
  invalidated, so a logged-out session cannot be replayed.
- **Frontend:** `<ProtectedRoute>` redirects unauthenticated visitors to
  `/login?next=…`. On page load, an expired access token is silently refreshed
  once via `POST /api/apps/{appId}/auth/refresh` before retrying `me()`.
- **Password policy:** minimum 10 characters, at least one letter and one digit
  (configurable).
- **Rate limits** (in-memory, per IP): login 5/min, register/OTP/reset 3 per
  10 min. Set any to `0` to disable.

### Configuration

See `backend/.env.example` under the `===== Auth =====` section. Key settings:

| Setting | Default | Notes |
|---|---|---|
| `ACCESS_TOKEN_EXPIRE_MINUTES` | 15 | access JWT lifetime |
| `REFRESH_TOKEN_EXPIRE_DAYS` | 30 | refresh token lifetime |
| `PASSWORD_MIN_LENGTH` | 10 | minimum password length |
| `PASSWORD_REQUIRE_LETTER` / `PASSWORD_REQUIRE_DIGIT` | true | complexity flags |
| `RATE_LIMIT_LOGIN_PER_MIN` | 5 | login attempts per IP per minute |
| `RATE_LIMIT_REGISTER_PER_10MIN` | 3 | registrations per IP per 10 min |
| `RATE_LIMIT_OTP_PER_10MIN` | 3 | OTP requests per IP per 10 min |
| `RATE_LIMIT_RESET_PER_10MIN` | 3 | password resets per IP per 10 min |

### Migrations

The `refresh_tokens` + `revoked_tokens` tables are created by Alembic revision
`031` (chains off `030`). Apply migrations with:

```bash
cd backend && python -m alembic upgrade head
```

### Deployment notes

- Backend code changes require **restarting both** `zhanlu-backend` and
  `zhanlu-sandbox-worker` — bind-mounted code is only picked up at process start.
- Dashboard generation (`create_fullstack_dashboard` / `update_fullstack_dashboard`)
  executes **in-process** in the backend via `DashboardAppGenerator` — it does
  not route through the sandbox.
- Frontend-only changes need no container restart (dist is bind-mounted `:ro`).

## Documentation

The `docs/` folder contains the full design corpus: `00_INDEX.md` is the entry
point, followed by the API contract (`01_api_contract/`), database schema
(`02_database/`), runtime contracts (`03_runtime_contracts/`), sandbox and
artifact specs (`04_sandbox_artifacts/`), architecture layers
(`07_existing_architecture/`), and final UI decisions (`09_final_ui_decisions/`).

## License

See `LICENSE`.
