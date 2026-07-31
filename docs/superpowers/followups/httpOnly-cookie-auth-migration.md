# Follow-up: httpOnly-cookie auth migration

**Status:** Not started. Recorded as future work.
**Context:** Plan `docs/superpowers/plans/2026-07-27-claude-style-auth-hardening.md` (Task 21).

## Why this exists

The 2026-07-27 auth hardening kept the JWT transport that `@base44/sdk` provides:
access + refresh tokens are stored in `localStorage` and sent as
`Authorization: Bearer <token>` by the SDK's internal axios client. That client
is **not exposed** by `@base44/sdk`'s `createClient()` (see
`node_modules/@base44/sdk/dist/client.js` — `axiosClient` is a closure local), so:

1. Tokens in `localStorage` are reachable by any XSS payload.
2. A mid-session 401 (access token expired mid-page) can't be intercepted +
   retried transparently — only the **page-load** path (`AuthContext.checkUserAuth`)
   does a silent refresh. Ongoing SDK calls (`base44.entities.*`,
   `base44.agents.*`, …) surface the 401 to the user until the next reload.

## Goal

Move both tokens into `httpOnly` + `SameSite=strict` cookies so JavaScript can't
read them, and have the backend read the token from the cookie (with the
`Bearer` header as a fallback during the transition). Adds a true axios
response interceptor that retries once on 401 after refreshing.

## Files that must change

### Backend
- `app/deps.py` — read the access token from the `access_token` cookie OR the
  `Authorization` header (fallback). Add a `set_auth_cookies(response, access, refresh)`
  helper.
- `app/routers/auth.py` — `login`/`register`/`refresh` must `set_cookie(...,
  httponly=True, samesite="strict", secure=...)` on the response instead of
  (or in addition to) returning the tokens in the JSON body.
- `app/config.py` — add `COOKIE_DOMAIN`, `COOKIE_SECURE` (true in prod).

### Frontend
- `node_modules/@base44/sdk/dist/utils/axios-client.js` — **fork** (or monkey-patch
  at app startup) so `withCredentials: true` is set and the SDK stops managing
  the token in `localStorage`. This is the load-bearing change.
- `frontend/src/lib/AuthContext.jsx` — stop reading/writing `localStorage.token`
  / `refresh_token`; rely on cookies. Keep the 401-refresh logic but move it into
  a real axios interceptor (now possible because the forked axios client is
  accessible).
- `frontend/src/pages/Login.jsx`, `Register.jsx` — stop persisting tokens
  manually; the backend sets the cookies.
- `frontend/src/lib/app-params.js` — drop the `access_token` app-param plumbing.

## Acceptance criteria
- `localStorage` contains no auth tokens after login.
- An XSS `localStorage` dump returns nothing useful.
- A 15+ minute idle then an in-app action silently refreshes and succeeds
  (no visible error, no full reload).
- `document.cookie` from the JS console cannot read the access/refresh tokens.

## Risk

Forking the SDK means owning its updates. Prefer a thin monkey-patch at app
bootstrap (override `axiosClient.interceptors` + `withCredentials`) over a
vendored fork if the SDK's surface allows it.
