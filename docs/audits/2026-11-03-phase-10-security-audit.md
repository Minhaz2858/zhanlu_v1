# Phase 10 — Enterprise Security & Tenant Isolation: Audit + Spec

**Date:** 2026-11-03 · **Branch:** `fix/automation-chat-artifacts-ordering` · **Base:** `249fb7a4b` (Phase 9 closed)

Evidence from four parallel read-only audits (file:line citations below). Foreign
WIP checked before planning: only `delegate_tool.py` (M) and
`agent_collaboration/broker.py` (untracked) are dirty among all targets.

## Workstream 1 — Sandbox

| # | Sev | Class | Finding |
|---|-----|-------|---------|
| S1.1 | P0 | CONFIRMED | `sandbox_worker/main.py:149-154,317-349` — on Docker unavailability the worker executes jobs **in-process on the host** (`_execute_in_process`, "no isolation guarantees"). The client runner was already hardened (audit D2); the worker was not. |
| S1.2 | P0/P1 | CONFIRMED | `workflow_sandbox.py:157-167,218-220` — workflow Code-node user code falls back to `_restricted_exec` **in the backend process** when enqueue/poll fails. Restricted `__builtins__` is not a CPython security boundary. |
| S1.3 | P1 | CONFIRMED | `sandbox/runner.py:114-140` + `config.py:2177` — host-execution fallback gated only by `SANDBOX_ENABLED` (default **False** → host exec by default). `:382-388` bash "runtime" is unrestricted host bash on the fallback path. |
| S1.4 | P1 | CONFIRMED | `container_manager.py:149-159` — `extra_mounts` accepts **arbitrary host paths** into the container (no allowlist). |
| S1.5 | P1 | CONFIRMED | `main.py:256-257` + `sandbox_code.py:53-55` — job `env_vars` injected verbatim into `docker run -e`; shared unauthenticated LLM proxy socket (`llm_proxy.py:134`) holds the real API key. |
| S1.6 | P2 | CONFIRMED/SUSPECTED | `:latest` image tags (no digest pinning); world-writable `SANDBOX_TMP_ROOT` (`chmod 0o777`) shared across jobs on the host. |

Container invocation itself is well hardened: `container_manager.py:132-147` has
`--network none`, `--read-only`, `--memory`, `--cpus`, `--cap-drop ALL`,
`no-new-privileges`, `--pids-limit` (default 64), timeout `rm -f` cleanup.

**Plan:** fail-closed on S1.1/S1.2 (refuse host execution, fail the job), opt-in
flag for the runner fallback (S1.3: `SANDBOX_ALLOW_HOST_FALLBACK`, default
false), tmp-root allowlist for `extra_mounts` (S1.4). **Deferred with owner:**
S1.5 (env allowlist + proxy auth — needs proxy design decision), S1.6 (hardening
backlog). `code_execution_tool.py:327-334` host exec (tests pin it by design) —
**explicitly deferred**: routing it through the sandbox worker is feature work;
documented accepted-risk, owner: platform security.

## Workstream 2 — Skills tenancy

| # | Sev | Class | Finding |
|---|-----|-------|---------|
| S2.1 | P0 | CONFIRMED | `tools`/`skill_profiles` tables have **no org column at all** (`models/tool.py:9-68`); `SkillsRegistry` is a global name-keyed singleton over `skills/` + `~/.zhanlu/skills/` ("single-tenant data" comment, `skills_loader/__init__.py:535`); all tenants share one physical dir (`skill_sync.py:27`). |
| S2.2 | P1→CORRECTED | PARTIAL | Re-verification during implementation: `marketplace.py:391-400` DOES enforce an owner check (403 "Not your skill") — the original IDOR reading was wrong. Pinned by `test_marketplace_skill_ownership.py`. Residual: `delete_skill_md` deletes by NAME in the shared directory, so same-named skills of different users collide — that residual is part of the S2.1 file-layer problem (per-org dirs), same blocked item. |
| S2.3 | P1 | CONFIRMED | Execution has no ownership to check against (`skills_tool.py:74,106,371-444`); zero tenant tests across ~50 skill test files. |

**Plan:** fix S2.2 now (owner check, fail closed). **S2.1/S2.3 = BLOCKED with
spec**: schema-level tenancy (org column migration + per-org skill dirs +
registry-per-tenant) is a broad refactor over ~50 test files and the actively
foreign-WIP skills area; recorded as the P0 spec for a dedicated workstream
(schema migration `tools.org_id`, per-org `~/.zhanlu/skills/<org>/`, caller-org
check in `skills_tool` load/execute, cross-tenant denial tests).

## Workstream 3 — Entity API

| # | Sev | Class | Finding |
|---|-----|-------|---------|
| S3.1 | P1 | CONFIRMED | `artifacts.py:1230-1280` — brand-kit GET/PUT/DELETE have **no auth dependency** and trust client `org_id` query param → unauthenticated cross-org read/write. |
| S3.2 | P1 | CONFIRMED | `agent_tools.py:1692,1850,2032` — `args.get("org_id") or ...` lets tool-call arguments override the trusted org context (client-controlled identity vector). |
| S3.3 | P2 | SUSPECTED | `entity_service.py:616-641,793-841` — `is_system` rows deletable-guarded but **updatable** by any org user. |

E2 itself: **already fixed** — `entities.py:244-258` org from authenticated
principal, `_IMMUTABLE_FIELDS` strips client `org_id` (`entity_service.py:249,
280-292`), `_tenant_filters` on all paths. Plan: fix S3.1 + S3.2, add
router-layer cross-org denial tests (currently service-layer only), guard
`is_system` updates (S3.3, small).

## Workstream 4 — MCP session isolation

| # | Sev | Class | Finding |
|---|-----|-------|---------|
| S4.1 | P1(med) | CONFIRMED | `fastmcp_app.py:76-77` — non-POST requests (GET SSE stream, DELETE session) pass **unauthenticated**: a captured `mcp-session-id` can observe another principal's session event stream or terminate their session. `tools/call` itself is per-request authenticated (defense effective). |
| S4.2 | P2 | SUSPECTED | `mcp_auth.py:91-92` — contextvar principal never reset (defensive hygiene). |

Phase 8.2 authentication is NOT rebuilt — these are additive hardening. Plan:
require Bearer auth on non-POST requests carrying `mcp-session-id`; reset the
contextvar defensively.

## Workstream 5 — Authorization boundaries

| # | Sev | Class | Finding |
|---|-----|-------|---------|
| S5.1 | P1 | CONFIRMED | `delegate_tool.py:133-141` `_resolve_agent_app` resolves delegation targets by name+published **without org filter** (cross-org AgentApp prompt/tool_config loadable; same-name resolution nondeterministic). Broker path has no org check either (`broker.py` — foreign WIP). |
| S5.2 | P1 | CONFIRMED | `agent_tools.py:635-636` — the central tool-policy gate **fails open** on gate errors. |
| S5.3 | — | CONFIRMED EFFECTIVE | v3 stream org wall (`agents.py:13096-13107`, fail-closed 403), conversation ownership (`:12183-12184`), execution_recovery is a failure classifier not an authz point (by design). No work. |

**Plan:** org wall in `_resolve_agent_app` (surgical on foreign-dirty file);
fail-closed central policy gate.

## Execution order

W1 sandbox → W5 delegate/dispatch → W3 entity/artifacts → W4 MCP → W2 skills
IDOR → cross-tenant regression suite → docs/commits. Each fix TDD where a
deterministic test is possible.
