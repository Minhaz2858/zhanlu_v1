# Phase 8.2 — External MCP Authentication Specification

**Date:** 2026-11-03 · Prerequisite: `docs/audits/2026-11-03-phase-8-2-external-mcp-security-audit.md`

## 1. Credential contract (smallest secure addition — reuses the existing JWT machinery)

- **Format:** a Zhanlu access JWT issued by the EXISTING `auth_service.create_access_token` machinery, optionally with long expiry for MCP use: payload carries `sub` (user id), `jti`, `type: "access"`, `org_id`, `role`. Long-lived MCP tokens use the SAME format and secret with `exp` set at issuance (90 days) — no new crypto, no new token format.
- **Issuance:** `POST /api/mcp/tokens` (authenticated via the standard `Authorization: Bearer <access JWT>`; requires a live user) — issues a personal MCP token, returns it once. Issuance is audited. Listing/revoking a user's own tokens via the same endpoint family.
- **Validation:** `Authorization: Bearer <token>` header → `auth_service.verify_token_claims` (signature, expiry, JTI revocation blacklist) → load the `User` row from the DB (authoritative org/role; deleted/disabled users fail).
- **Expiration:** standard JWT `exp` — expired ⇒ 401.
- **Revocation:** JTI into the existing `RevokedToken` blacklist ⇒ 401. Deleting/deactivating the user ⇒ 401 (DB row is the source of truth).
- **Malformed/missing header:** 401 before any privileged execution.

## 2. Trust boundary and request lifecycle

```
External MCP client                    Zhanlu backend
------------------                     ------------------------------------------
POST /api/ext-mcp/mcp
  {jsonrpc: initialize|tools/list} --> [ASGI auth middleware]
                                        public methods -> FastMCP (negotiation only)
  {jsonrpc: tools/call, ...}      ----> [ASGI auth middleware]
  Authorization: Bearer <mcp JWT>        verify_token_claims (sig/exp/JTI blacklist)
                                         -> load User row (authoritative org)
                                         -> authenticated principal (contextvar)
                                      --> FastMCP _tool
                                         - identity = contextvar principal (authoritative)
                                         - _zhanlu_context claims MUST match principal
                                           (mismatch -> context dropped)
                                         - agent authorization: claimed agent must be a
                                           live AgentApp of the principal's org
                                         - bound_kb_ids resolved from that agent's
                                           ENABLED AgentDataBindings (agent_binding_service)
                                         - zhanlu.call_tool (existing scoping applies)
                                         - audit events (no secrets)
```

## 3. What is public vs privileged

- **Public (unauthenticated):** MCP protocol negotiation — `initialize`, `notifications/initialized`, `tools/list`, `ping`, GET/SSE stream establishment. Exposes static tool names/descriptions only (audit F5). Required for spec-compliant clients that handshake before sending credentials context.
- **Privileged (auth required):** `tools/call` — 401 JSON-RPC error if credentials missing/invalid/expired/revoked. No anonymous privileged access exists anywhere.

## 4. Error contract

| Condition | Response |
|---|---|
| Missing Authorization on tools/call | 401, JSON-RPC error `-32001 "authentication required"` |
| Malformed header | 401 `-32002 "malformed credentials"` |
| Invalid/expired/revoked token | 401 `-32003 "invalid or expired credentials"` |
| Cross-org / unauthorized agent or datasource during call | tool-level honest refusal (`{"error": ...}` inside the result; audit denial event) |

All error bodies are static strings — no token fragments, no header echoes, no exception internals.

## 5. Context field authority (Stage 4 contract)

| Field | Authority |
|---|---|
| `org_id`, `user_id` (authenticated principal) | **authoritative** — from the verified DB User row; client CANNOT override |
| `_zhanlu_context.org_id` / `.user_id` | informational; must EQUAL the principal else the whole context is dropped |
| `_zhanlu_context.agent_app_id` | request-scoped agent selection; must be a live same-org AgentApp (else context dropped) |
| `run_id`, `conversation_id`, `app_id`, `agent_name` | informational trace refs |
| bound resources | NEVER from the client — resolved from enabled `AgentDataBinding` rows of the verified agent |

## 6. Sequence — privileged data tool call

```
client -> POST tools/call (Bearer MCP-JWT, _zhanlu_context:{agent_app_id})
middleware: verify_token_claims -> User(u1, org-ok)         [401 on any failure]
middleware: set principal contextvar; audit auth_success
_tool: extract claims; verify_mcp_context(db, claims merged with principal)
         - claimed agent must be live + org-ok (else context dropped)
       - bound_kb_ids := enabled bindings of agent (agent_binding_service)
       - context := {org_id: org-ok (authoritative), bound_kb_ids, ...}
       - audit tool_execution
zhanlu.call_tool -> handler scopes ALL datasource access to bound_kb_ids
```

## 7. Threat model (abridged)

- **Stolen MCP token:** revoke via JTI blacklist; org scoping limits blast radius; audit shows use.
- **Forged `_zhanlu_context`:** claims cannot widen — must match principal; agent must be same-org; resources resolved from bindings only.
- **Anonymous probing:** negotiation-only; privileged methods 401; generic error strings.
- **Cross-org enumeration:** impossible — every org reference is derived from the principal's DB row.
- **Secret leakage:** no tokens/headers logged; audit details carry ids and hashes only.

## 8. Acceptance criteria

1. `tools/call` without valid credentials ⇒ 401 (all transports).
2. `initialize`/`tools/list` succeed anonymously (negotiation only).
3. Valid MCP token ⇒ identity = DB User; org = user's org (client cannot override).
4. `_zhanlu_context` mismatching the principal is dropped entirely.
5. Datasource access only via enabled same-org bindings of the verified agent.
6. Auth success/failure/denial/execution audited with correct org attribution and no secrets.
7. All existing MCP/binding/runtime regressions stay green.
