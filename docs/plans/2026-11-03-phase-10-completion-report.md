# Phase 10 — Enterprise Security & Tenant Isolation: Completion Report

**Date:** 2026-11-03 · **Branch:** `fix/automation-chat-artifacts-ordering` · **Base:** `249fb7a4b`

Audit + spec: `docs/audits/2026-11-03-phase-10-security-audit.md` (4 parallel read-only
audits; every finding carries file:line evidence and a CONFIRMED/SUSPECTED class).

## Commits

| Commit | Workstream |
|--------|-----------|
| `bd7ad3251` | W1 sandbox: host-execution fallbacks fail closed + extra-mount allowlist |
| `a62d5400f` | W5 delegation org wall + fail-closed policy gate + org-override strip (surgical staging on foreign-dirty files) |
| `4dafc37ba` | W3 brand-kit endpoints authenticated + HTTP-layer cross-org denial tests |
| `b5683dfaf` | W4 MCP session-id is not a credential; principal hygiene |
| (this commit) | W2 skills ownership pin + audit correction + this report |

## P0/P1 findings — final disposition

| Finding | Sev | Disposition |
|---------|-----|-------------|
| S1.1 worker in-process fallback | P0 | **FIXED** — fails the job unless `SANDBOX_ALLOW_HOST_FALLBACK=true`; 2 tests |
| S1.2 workflow restricted-exec fallback | P0/P1 | **FIXED** — same flag gates both fallback points; 3 tests |
| S1.3 runner host-subprocess default-on | P1 | **FIXED** — explicit opt-in required; 2 tests; legacy fallback tests opt in explicitly |
| S1.4 arbitrary extra_mounts | P1 | **FIXED** — allowlist = `SANDBOX_TMP_ROOT` + LLM proxy socket; 1 test |
| S1.5 env injection + unauth LLM proxy | P1 | **DEFERRED with owner** — needs proxy auth design decision; documented |
| S1.6 image pinning, tmp-root perms | P2 | **DEFERRED** — hardening backlog |
| S2.1 skills schema tenancy (no org column, global dirs) | P0 | **BLOCKED with spec** — requires schema migration + per-org dirs + registry-per-tenant; broad refactor over ~50 test files and the foreign-WIP skills area; owner: skills/platform track; full spec in audit doc |
| S2.2 marketplace IDOR | P1 | **CORRECTED** — owner check exists (403); pinned by test; name-collision residual folded into S2.1 |
| S3.1 unauthenticated brand-kit + client org | P1 | **FIXED** — auth required, org from principal; structural pin test |
| S3.2 args org_id override | P1 | **FIXED** — 3 sites now context-only; test |
| S5.1 delegation cross-org resolution | P1 | **FIXED** — org-scoped resolution + fail-closed block on unresolvable caller; 4 tests (surgical staging: 3 of 23 hunks of the foreign-dirty delegate_tool.py) |
| S5.2 policy gate fail-open | P1 | **FIXED** — gate errors deny (fail closed); 1 test |
| S4.1 session-id observing/DELETE | P1(med) | **FIXED** — session-bearing non-POST requires Bearer; 2 tests; bare probes stay public |
| S4.2 contextvar reset | P2 | **FIXED** — reset_principal in middleware finally |

Verified non-findings (no work needed): E2 entity API org scoping (fixed
2026-10-22, now with HTTP-layer denial tests), v3 stream org wall (fail-closed
403), conversation ownership, `--network none`/`--pids-limit`/cap-drop already
present in container invocation.

## Verification evidence

- **Security regression: 94 passed** — all Phase 10 suites (sandbox fail-closed
  8, delegate org wall 4, dispatch boundaries 2, entity router cross-org 3,
  marketplace ownership 1, MCP auth 33 incl. 4 new, sandbox legacy 43) in one run.
- All edits on foreign-dirty files (`delegate_tool.py`, `agent_tools.py`) were
  staged surgically (marker-filtered hunks); commit contents audited to contain
  only Phase 10 changes.
- Phase 8.2 MCP authentication contract unchanged and green.
- No Phase 8.3 automation files touched.

## Remaining risks (explicit owners)

1. **S2.1 skills schema tenancy (P0)** — owner: skills/platform track; blocked
   on scope approval (migration + per-org dirs); the single largest residual.
2. **S1.5 sandbox env/proxy hardening (P1)** — owner: platform security.
3. **Full-sweep order dependence (from Phase 9)** — owner: test-infra track.
