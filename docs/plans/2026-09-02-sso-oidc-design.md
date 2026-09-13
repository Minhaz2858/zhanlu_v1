# SSO / OIDC — Design

> **Date:** 2026-09-02
> **Status:** Approved (user: "let's move to Next: item 7" — roadmap design)
> **Scope:** OIDC authorization-code flow (Google/Azure/Microsoft/Okta-class IdPs). SAML + SCIM = Phase 2 (documented, out of scope). Local login unaffected when `SSO_ENABLED=false`.

## Decisions

- **Browser-native authorization-code flow** (no frontend exchange leg): `authorize` (302 → IdP) → IdP redirects to `callback` → backend exchanges code, verifies the ID token, issues the normal access/refresh pair, then 302s to `${FRONTEND_URL}/sso/callback#access_token=...&refresh_token=...&user=...` — tokens live in the URL **fragment only** (never in query strings / server logs).
- **Reuse the existing OAuth client** (`app/services/mcp_client/oauth.py`): `build_authorize_url` + `exchange_code` are OIDC-compatible. New `app/services/sso/oidc.py` adds OIDC discovery (`{issuer}/.well-known/openid-configuration`), JWKS fetch (httpx), and ID-token verification (PyJWT 2.10: signature via JWKS, `iss`, `aud`, `exp`, `nonce`).
- **In-memory state store** (`sso/state.py`): `state` → `{provider, nonce, next, created_at}` with TTL (`SSO_STATE_TTL_S=600`), mirroring the existing `_PENDING_OAUTH` pattern in `routers/mcp.py`. Single-process limitation documented; Redis-backed store = Phase 2.
- **Account linking** (`sso/account.py`): find-or-create by **verified email**. Existing local user → just logs in (linked). New user → created with an unusable random `password_hash` + `full_name` from claims (`sub`/`name`). `SSO_ALLOWED_EMAIL_DOMAINS` allowlist (empty = allow all; host + subdomain match, same semantics as `tool_security.is_url_in_allowlist`).
- **ID-token claims contract:** `email` required; `email_verified != False`; `nonce` must match the authorize-time nonce.
- **Config:** `SSO_ENABLED=false`, `SSO_PROVIDERS=[]` (JSON list of `{id, name, client_id, client_secret, issuer, discovery_url?, scopes?}`), `SSO_ALLOWED_EMAIL_DOMAINS=[]`, `SSO_STATE_TTL_S=600`. Secrets only in env/`.env` — never logged.
- **Routes** (new `app/routers/auth_sso.py`, mounted at `/api`): `GET /api/apps/{app_id}/auth/sso/providers` (frontend button list), `GET .../sso/{provider}/authorize?next=` (302), `GET .../sso/{provider}/callback?code&state` (tokens → frontend fragment). Unknown provider / SSO disabled / bad state → error redirect or 404/403 (no tokens).
- **Frontend:** Login/Register render "Continue with <provider>" buttons from `/auth/sso/providers`; new `SsoCallback` page at `/sso/callback` stores the fragment tokens via `base44.auth.setToken` + `localStorage.refresh_token` (same as `finishLogin`), then honors `?next=` / role-based landing.

## Files

- **New:** `backend/app/services/sso/{__init__,oidc,state,account}.py`, `backend/app/routers/auth_sso.py`, `backend/tests/test_sso_oidc.py` (mocked discovery/JWKS/token HTTP — no live IdP), `frontend/src/pages/SsoCallback.jsx`, `docs/plans/2026-09-02-sso-oidc-design.md`.
- **Modified:** `backend/app/config.py`, `.env.example`, `backend/main.py` (mount router), `frontend/src/pages/Login.jsx`, `Register.jsx`, `frontend/src/App.jsx` (route), roadmap doc.

## Acceptance

1. `SSO_ENABLED=false` → `/auth/sso/providers` returns `[]`; local email/password login untouched.
2. Authorize with a configured provider → 302 to the IdP with `state` + `nonce`; callback with a valid code + state → verified ID token → find-or-create user → working access/refresh tokens (fragment redirect).
3. Bad signature / wrong `aud` / wrong `iss` / expired / nonce mismatch → rejected (error redirect, no user created).
4. New user from an allowlisted domain is created; disallowed domain is rejected; existing local user with the same email is linked (no duplicate).
5. Frontend builds cleanly (`vite build`).
