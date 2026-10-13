# Multi-Tenant SaaS: Auth & Org Isolation — Design

**Date:** 2026-10-13
**Status:** Approved in discussion (user picked: Option C fully + any-database/tenancy; multi-tenant SaaS; two roles)

## Context

Zhanlu becomes a multi-tenant SaaS platform. Tenant = Organization.
Three architecture decisions approved:

1. **Option C** — one orchestrator loop; the Synexia FSM becomes a
   capability (`query_database`); routing regexes retire; determinism
   lives below the loop. (Phased separately.)
2. **Tenancy** — shared platform DB with `org_id` on every table
   (already true via `TimestampedBase`) + Postgres RLS as the
   enforcement net + TenantContext resolved from the JWT.
3. **This spec: the auth layer** — the source of the tenant context.

## Auth design (this spec's scope)

### Identity → org resolution
- **The client never sends `org_id`.** Email is globally unique; the
  server resolves `org_id` from the user record and issues it in the
  JWT. Login form unchanged (email + password).
- JWT access tokens gain `org_id` + `role` claims (backward-compatible:
  tokens without claims still resolve org from DB).
- Multi-org membership: out of scope (YAGNI); the JWT shape supports
  adding an org-picker later without breaking anything.

### Roles — exactly two
- `admin` (Org Admin): provisioning, KB/datasource management, domain
  config, org LLM keys, audit/cost visibility, org-wide agent management.
- `user` (Normal): chat, own projects, org-shared agents/dashboards.
- Stored in the existing `users.role` column (values unchanged:
  "admin"/"user" — no migration, no frontend role remapping).
- A platform-level super-admin (SaaS operator) is a later concern; the
  first/default org's admin effectively holds it today.

### Org signup (SaaS self-serve)
- `POST /api/apps/{app_id}/auth/signup-org` —
  `{org_name, email, password, full_name?}` creates the Organization
  (unique slug) + the first `admin` user atomically.
- Gated by `ALLOW_ORG_SIGNUP` (default **false** → existing enterprise
  behavior unchanged; SaaS mode enables via env).
- Rate-limited like login. When disabled, the endpoint returns 403 with
  a clear message.

### Org-scoped admin surface
- `require_org_admin` dependency (role == "admin"); admin routers scope
  every query by `admin.org_id` — `list_users` returns only same-org
  users; create_user assigns the admin's org; get/update/delete of a
  cross-org user returns 404 (existence hidden).
- Login responses expose `org_id` + `role` explicitly (from the user
  row via to_dict) so the frontend can gate the admin console.

### Relationship to the tenancy plan
- TenantContext (later phase) = decode JWT → `{org, role, user}` →
  `SET app.current_org` for RLS. Auth is its single source.
- The signup endpoint is the onboarding step 1 of the tenancy design.

## Non-goals (this spec)
- RLS rollout, TenantContext threading, domain-config-per-org —
  separate phases (documented in the discussion 2026-10-13).
- SSO (auth_sso.py exists; untouched).
- Multi-org membership / org picker.

## Testing
- signup-org creates org + admin (and is 403 when disabled)
- login returns org_id + role
- admin list is org-scoped; cross-org user invisible (404)
- normal user gets 403 on admin surface
- existing tokens (no org claim) still authenticate
