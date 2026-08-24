# Zhanlu

FastAPI backend + React (Vite) frontend, glued together by the `@base44/sdk`.

## Authentication

Login is **required for every page and every mutating/LLM/data endpoint** — there
is no anonymous access (see `docs/superpowers/plans/2026-07-27-claude-style-auth-hardening.md`).

- **Methods:** email + password only. Registration verifies via an emailed OTP
  code (unless no users exist yet, for first-time setup).
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

### Migrations

The `refresh_tokens` + `revoked_tokens` tables are created by Alembic revision
`031` (chains off `030`). Apply with `python -m alembic upgrade head` against
the target database.

### Deployment

**HARD RULE — every backend deploy restarts BOTH backend containers:**

```bash
docker restart zhanlu-backend zhanlu-sandbox-worker
```

- Both containers bind-mount the same repo (`/home/ysk2025/zhanlu_7_30/backend` →
  `/app`), but a bind-mount **never reloads a running process's imported modules** —
  a process keeps the code it imported at startup until it is restarted.
- `zhanlu-backend` runs the API + agent FSM + the dashboard pipeline
  (`create_fullstack_dashboard` / `update_fullstack_dashboard` execute **in-process**
  here via `DashboardAppGenerator` — they do NOT route through the sandbox).
- `zhanlu-sandbox-worker` runs `python -m sandbox_worker.main`: it BRPOPs the Redis
  `sandbox:queue` (jobs enqueued by `SandboxService` from `run_sandbox_skill`) and
  spawns disposable Docker containers via the docker socket. A stale worker silently
  runs old skill-runner code even though the repo is already updated.
- Frontend-only changes need no restart (dist is bind-mounted `:ro`).

### Known follow-ups

- The access/refresh tokens live in `localStorage` (constrained by the
  `@base44/sdk` transport). Migrating to httpOnly cookies requires forking the
  SDK's axios client + rewriting `AuthContext.jsx` — see
  `docs/superpowers/followups/httpOnly-cookie-auth-migration.md`.
- Mid-session 401 auto-refresh (for ongoing SDK calls, not just page load) is
  part of that same follow-up.
