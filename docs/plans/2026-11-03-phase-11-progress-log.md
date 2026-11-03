# Phase 11 — Progress Log

**Branch:** `fix/automation-chat-artifacts-ordering` · **Start:** HEAD `ef010e042` (Phase 10 complete)

## Stage 0 — Repository protection and inventory (2026-11-03)

- HEAD verified: `ef010e042` with all Phase 9/10 commits present.
- Working tree: 280 foreign-WIP entries (up from 276) — the Phase 8.3
  automation workstream and other sessions are actively editing. UNTOUCHED.
- Foreign untracked Alembic migrations observed: 106, 110, 115, 118, 126,
  127, 128, 130, **132_automation_retry_scheduling (current head)** — any new
  migration must chain from 132.
- Skills subsystem files (`models/tool.py`, `models/skill_profile.py`,
  `services/skill_sync.py`, `services/skills_loader/`, `routers/marketplace.py`,
  `routers/skill_studio.py`, `tool_handlers/skills_tool.py`): **clean at
  HEAD** — fully ownable.
- Foreign-dirty files relevant here: `app/services/tool_handlers/delegate_tool.py`,
  `app/services/agent_tools.py`, `app/models/agent_app.py`,
  `app/models/knowledge_base.py` (check per-edit), `services/project_kb_membership.py`
  (untracked foreign WIP — migration-020 collision).
- Read: Phase 9 audit + completion report (incl. closure addendum), Phase 10
  audit + completion report. Carried-over item list confirmed.
- No production code modified in this stage.

## Investigation dispatches (parallel, read-only)

- A1: skills tenancy deep-dive (models → registry → sync → callers → tests).
- A2: sandbox env injection + LLM proxy auth + P2 hardening evidence.
- A3: migration-020 drift inventory (models, queries, live callers, foreign WIP).
- A4: test-order contamination source hunt (document-family region) + observability audit.

*(findings appended below as they land)*

## Investigation results (2026-11-03)

### A1 skills (S1 design inputs)
- **`Tool.org_id` ALREADY EXISTS** (TimestampedBase, default "default-org") — never populated from tenant context nor filtered by any skill query. Highest-leverage hook.
- `Tool.source` distinguishes system tiers: `builtin` (seed), `marketplace` (sync), `marketplace_user` (per-user install), `custom`. SkillMetadata.source marks FS origin ("bundled"/"user").
- `tenant_context.get_tenant_org()` is populated for every request by TenantContextMiddleware (JWT org claim) and reachable inside skills_tool/marketplace with ZERO signature changes.
- skills_loader DB lookups (4 sites) filter only name+is_deleted+enabled — the wall to add.
- BUG: `marketplace.py:279` passes `source=` to `write_skill_md` which has no such param → TypeError swallowed → marketplace install's FILE write silently fails (DB row only).
- Registry: global name-keyed singleton; per-org overlay must NOT re-scan everything — cache per org.
- Load-bearing tests: test_skills_e2e, test_skill_marketplace_lifecycle, test_skill_source_*, test_get_or_install_skill, test_skill_delete (+~60 more).

### A2 sandbox env/proxy (S2)
- Worker `input_package["env_vars"]` has NO producer today (dead weight); the only live free-form env path is `sandbox_code`'s `env` arg → runner → `docker -e` (in-process fallback merges into clean_env).
- LLM proxy: socket chmod 0666, no client auth, rate limit keyed per-CONNECTION (reconnect = fresh bucket). Cleanest per-job auth: worker mints `LLM_PROXY_TOKEN = HMAC(job_id, secret)`; proxy verifies (shares DB/settings).
- P2: all images `:latest` (config 2227-2229 + worker IMAGE_MAP + skills_tool hardcode); tmp root chmod 0777; TemporaryDirectory not cleaned on SIGKILL.

### A3 migration-020 (S3)
- ALL 5 models still declare `project`; migration 032 RE-ADDS the column to all 5 tables (so DB presence is expected!).
- **main.py:451-474 startup backfill reads `t.project` for all 5 tables — dropping the column breaks startup.**
- Live reads: KnowledgeBase.project ×4 (project_kb_membership is LIVE — 9 call sites incl. routers), ChatSession.project ×1 write (automation_sessions:102). AgentApp/UserFile/AutomationTask.project: no app writes (AutomationTask has one serializer read at automation_api:330).
- Correct target: keep `project` for KnowledgeBase + ChatSession (load-bearing); safe to drop for AgentApp/UserFile/AutomationTask ONLY after retiring main.py backfill + serializer. The "drift" resolves as a DOCUMENTED CONSISTENCY CONTRACT, not a blind column drop.

### A4 test-order + observability
- Deterministic repro candidate: `TOOL_CONTEXT` module dict leaks `chat_session_id` from tests/test_automation_runtime_agent.py:181-182 (finally pops only agent_app_id). Repro: `pytest tests/test_automation_runtime_agent.py tests/test_dashboard_binding.py tests/test_document_access.py -x -q`.
- Other global-state candidates: tenant_context ContextVar (no token reset), routing_feedback._CACHE, shared_evidence _STORE.
- Observability: AuditEvent exists (22 sites) but **auth login/register/refresh writes ZERO audit rows**; /healthz is static (no DB check); no structured logging.

## Disposition decisions
- S1: implement org scoping via existing Tool.org_id + tenant_context + per-org FS overlay + migration 133 + legacy file migration script. System tier = bundled/builtin/marketplace sources.
- S2: env allowlist at runner + worker; per-job HMAC proxy token (LLM_PROXY_TOKEN).
- S3: document the schema contract; NO blind column drop (KB/ChatSession load-bearing); retire what is safely retireable; align test_020 with the documented contract.
- S4: fix TOOL_CONTEXT leak + autouse global-state reset in conftest; verify with the repro command; then full sweep.
