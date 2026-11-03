# Phase 8.2 — External MCP Authentication, Authorization & Security Hardening

**Date:** 2026-11-03 · **Branch:** `fix/automation-chat-artifacts-ordering` · **Base:** `4c370c34d` (Phase 8.1)

**Commits:**
- `6c5fcc8d9` — Stages 0-1: security audit doc + auth architecture spec
- `c25155023` — Stages 2-6: authentication, authorization boundary, audit, tests

---

## Stage 0 — Read-only security audit

`docs/audits/2026-11-03-phase-8-2-external-mcp-security-audit.md`

| # | Finding | Classification |
|---|---------|----------------|
| F1 | `/api/ext-mcp` has no authentication — any network caller can invoke all 197 tools | CONFIRMED / CRITICAL |
| F2 | `_zhanlu_context` claims not anchored to an authenticated principal | CONFIRMED / HIGH |
| F3 | No audit events on the external MCP path | CONFIRMED / MEDIUM |
| F4 | Raw exception text reachable from protocol layer | CONFIRMED / LOW |
| F5/F6 | Transport/session concerns | NOT A DEFECT (as designed) |
| F7 | SSE session reuse across identities | SUSPECTED (hardened transitively) |
| F8 | Long-lived credential story absent | DEFERRED (addressed by this phase's token API) |

## Stage 1 — Auth architecture spec

`docs/superpowers/specs/2026-11-03-phase-8-2-external-mcp-authentication.md`

Key decisions: reuse the existing JWT machinery (no new crypto/identity platform);
90-day MCP tokens via extended `exp`; public protocol negotiation separated from
privileged execution; error contract `-32001` (auth required) / `-32002` (invalid
context) / `-32003` (denied).

## Stages 2-4 — Implementation

**New: `app/services/mcp_server/mcp_auth.py`** — single security module:
- `authenticate_mcp_request(db, authorization)` — Bearer JWT validated with the
  existing machinery (`verify_token_claims`: signature + expiry + JTI revocation
  blacklist) and anchored to the DB `User` row; fail-closed (`None` on anything).
- `is_privileged_jsonrpc(body)` — only `tools/call` (including batched) is
  privileged; `initialize` / `tools/list` / notifications / `ping` pass through;
  unparseable bodies pass through public (they cannot contain `tools/call` and
  the protocol layer rejects them).
- `principal_var` ContextVar — per-request identity, no global state.
- `extract_zhanlu_context` / `verify_mcp_context` (moved here from
  `fastmcp_app.py` to break a circular import; re-exported for compatibility).
- `resolve_execution_context(db, arguments, principal)` — **the** authorization
  choke point: principal identity is authoritative; mismatching `org_id` /
  `user_id` claims drop the whole context; anonymous callers get no context at
  all (client claims carry zero identity authority); claimed `agent_app_id` is
  verified against the **principal's** org — a cross-org agent never enters the
  execution context; `bound_kb_ids` come ONLY from enabled `AgentDataBinding`
  rows (`get_bound_data_source_ids_via_bindings`) — never from the client, never
  a default datasource, never query-all.

**New: `McpAuthMiddleware`** (in `fastmcp_app.py`) — pure-ASGI, transport-level
(wraps `streamable_http_app()`, so every transport inherits protection). Buffers
the POST body, classifies the JSON-RPC method, authenticates privileged calls
BEFORE execution, stores the principal in the ContextVar, replays the body.
401 short-circuit with a static JSON-RPC error — never anonymous privileged
access, no exception text leakage.

**New: `app/routers/mcp_tokens.py`** — `POST /api/mcp/tokens` (issue, `days`
default 90 / max 365, caller-authenticated via standard deps) and
`POST /api/mcp/tokens/revoke` (self-only; cross-user attempt → 403 + audited).
Registered in `main.py` (surgically staged — foreign WIP hunks untouched).

**`app/models/audit_event.py`** — added `"mcp"` event source and
`mcp_auth_success` / `mcp_auth_failed` / `mcp_tool_invoked` event types.

## Stage 5 — Audit events

`mcp_auth_failed` (middleware 401), `mcp_auth_success`, `mcp_tool_invoked`
(per privileged tool call, org attributed from the verified principal),
`mcp_token_issued` / `mcp_token_revoked` / `mcp_token_revoke_denied` (router).
**No secrets logged** — ids and outcomes only; token material never enters any
audit detail.

## Stage 6 — TDD security matrix (RED→GREEN)

`tests/test_mcp_authentication.py` (9): valid / missing / malformed / garbage /
expired / revoked / deleted-user tokens; privileged-vs-public classification;
batched privileged call.
`tests/test_mcp_auth_middleware.py` (3): ASGI 401 short-circuit, public
pass-through.
`tests/test_mcp_token_issuance.py` (3): issue→use→audit (token material absent
from audit), revoke own, cross-user revoke denied.
`tests/test_mcp_authorization_boundary.py` (9): anonymous no-context; anonymous
claims carry no identity authority; authenticated identity overrides claims;
mismatching claims drop context; cross-org agent never enters context; enabled
binding drives scope; disabled binding → empty scope; no binding → no
default/query-all; client-supplied `bound_kb_ids` ignored.

**41 passed** across the four new suites (+ existing
`test_mcp_context_verification` / `test_mcp_context_propagation` /
`test_audit_event_registry` suites: 62 passed together).

## Stage 7 — Regression matrix

102 passed / 1 failed across `test_mcp_server`, `test_mcp_session`,
`test_mcp_client_transports`, `test_mcp_proxy`, binding suites
(`agent_binding_{api,service,model}`, `agent_datasource_wiring`,
`runtime_binding_resolution`), `datasource_pinning_org_wall`,
`test_audit_event_registry`. The single failure
(`test_list_tools_includes_multimodal`) is the **pre-existing, documented**
multimodal-handlers-absent failure from Phase 8.1's known list — unrelated to
this phase.

Dev-DB hygiene: three stray rows from an early test-helper session
(2 `audit_events` + 1 `revoked_tokens` for test user `u1`, leaked via a
dependency-override mismatch) were identified and deleted; no fixture entities
(user/org/agent) leaked.

## Security invariants — all enforced

- No client-supplied `org_id` / `user_id` / `agent_app_id` trusted as identity
- Fail-closed authentication (any doubt → 401)
- No cross-org access (agent claims verified against principal's org)
- No default datasource / query-all (scope = enabled bindings only)
- Public protocol negotiation separated from privileged execution
- No secrets in logs or audit events

## Deferred (documented, out of scope)

- Per-session binding of SSE transports to the authenticating identity beyond
  the per-request ContextVar (F7 residual)
- Token rotation/refresh UX and scoped (tool-limited) tokens
- Rate limiting on the MCP mount

## Next

Phase 8.3 per roadmap; auto-bind flag removal once migration criteria are met
(T8-12).
