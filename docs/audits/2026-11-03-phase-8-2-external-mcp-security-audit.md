# Phase 8.2 — External MCP Security Audit (Stage 0, read-only)

**Date:** 2026-11-03 · Branch `fix/automation-chat-artifacts-ordering` · HEAD `4c370c34d`
**Scope:** the complete `/api/ext-mcp` request lifecycle, existing identity primitives, and the authorization surfaces an external MCP caller can reach.

## 1. Request lifecycle (verified)

1. **Mount:** `main.py:1120` — `app.mount("/api/ext-mcp", get_mcp_http_app())`. The host app's router-level auth (`get_current_user_required`) does NOT apply to mounts; **no authentication middleware covers this path** (CONFIRMED — the critical gap).
2. **Transport:** `fastmcp_app.py:74` — FastMCP `streamable_http_app()` (spec transport: POST JSON-RPC + GET SSE stream on `/mcp`).
3. **Tool surface:** every ToolRegistry handler is exposed (`fastmcp_app.py:61-65`) — including data tools (`ask_data_agent`, `answer_from_database`, `execute_query`).
4. **Invocation:** `_tool` → `zhanlu.call_tool(name, args, context)` (`fastmcp_app.py:48+`) → registry handler with `context`.
5. **Context (8.1):** `extract_zhanlu_context` (allow-list) → `verify_mcp_context` (DB existence/ownership checks) — but the claims themselves are CALLER-SUPPLIED with no authenticated principal behind them (CONFIRMED).
6. **Data-tool scoping:** `ask_data_agent` validates `preferred_kb ∈ context["bound_kb_ids"]` and binds tool scope to `bound_kb_ids` (`delegation_tools.py:596-626`) — with no context, the no-context fallback yields NO bound sources (fail-honest; NOT A DEFECT for privilege, but means anonymous callers currently get empty-scoped data tools while other registry tools run unauthenticated).

## 2. Findings

| # | Class | Finding | Evidence | Impact | Correction |
|---|---|---|---|---|---|
| F1 | CONFIRMED / CRITICAL | No authentication on `/api/ext-mcp` — every registry tool (LLM-backed, data, ERP, agents) is invocable anonymously | `main.py:1120`; no auth wrapper in `fastmcp_app.py` | Unauthenticated compute/data access, cost/abuse, org enumeration via error messages | ASGI auth middleware; privileged JSON-RPC methods require verified credentials |
| F2 | CONFIRMED / HIGH | `_zhanlu_context` claims are DB-validated but not anchored to an authenticated principal — a caller may claim ANY existing org/user/agent triple | `verify_mcp_context` (`fastmcp_app.py`); `extract_zhanlu_context` allow-list | Cross-org access with known ids (ids are not secrets once known) | Claims must MATCH the authenticated principal (Stage 4) |
| F3 | CONFIRMED / MEDIUM | No audit events for MCP invocations or denials — auth attempts, cross-org attempts, and privileged calls are invisible | no AuditEvent write in `fastmcp_app.py`/`call_tool` | No forensic trail | AuditEvent at auth success/failure + tool denial/execution (Stage 5) |
| F4 | CONFIRMED / MEDIUM | `call_tool` failures return `{"error": str(exc)}` — raw exception text can leak internals (paths, SQL fragments) to anonymous callers | `mcp_server/__init__.py:69-71` | Information disclosure | Sanitize/generic error text for external callers |
| F5 | NOT A DEFECT | Anonymous `tools/list`/`initialize` exposure — tool NAMES and descriptions only; no org data | registry metadata is static | None once F1 enforces auth on `tools/call` | Public negotiation explicitly allowed (documented) |
| F6 | NOT A DEFECT | Data-tool scoping without context fails honestly (no bound sources) | `delegation_tools.py:596-626` | No privilege | Keep as anonymous fallback |
| F7 | SUSPECTED / LOW | SSE GET stream sessions persist across requests; a session opened by an authenticated request could theoretically serve later unauthenticated traffic on the same session id | MCP streamable-http session ids (`mcp` SDK) | Low (read-side transport) | Auth required on privileged methods regardless of session (covered by F1 fix); document |
| F8 | DEFERRED | Long-lived credential for external clients — access JWTs expire in minutes; MCP clients cannot do refresh flows | `auth_service.create_access_token` (ACCESS_TOKEN_EXPIRE_MINUTES) | Poor operability without weakening | Smallest secure addition: MCP-scoped long-lived JWT with JTI in the EXISTING revocation blacklist (Stage 1 spec) |

## 3. Existing identity primitives (reuse, no new system)

- `auth_service.create_access_token` / `verify_token_claims` (`auth_service.py:63-109`): JWT, `exp`, `jti`, org/role claims.
- Revocation: `RevokedToken` table + `is_access_token_revoked` (`:160`) — expiry AND revocation both fail closed.
- User identity: `User` (org_id, role); org membership is the DB row (source of truth).
- `AgentDataBinding` + `agent_binding_service`: authoritative per-agent resource authorization (Phase 8).
- `verify_mcp_context`: DB existence/ownership checks (8.1) — becomes the claim-MATCH layer in Stage 4.

**Authentication vs Authorization vs Context verification (distinct layers):**
- *Authentication* (Who?): MISSING for ext-mcp → Stage 2 adds Bearer JWT auth (existing JWT/revocation machinery).
- *Authorization* (What?): binding service + per-turn runtime scoping — must be driven by the AUTHENTICATED identity (Stage 3).
- *Context verification* (Are claims valid?): `verify_mcp_context` — extended so claims must MATCH the authenticated principal (Stage 4).

## 4. Regression tests required
Auth: missing/malformed/invalid/expired/revoked/valid tokens; public initialize vs privileged tools/call; all-transport coverage. Identity: cross-org agent/user claims rejected. Datasource: binding enforcement via authenticated agent context. Audit: events attributed, no secrets. Regression: F6 fallback preserved; no query-all/default datasource anywhere.
