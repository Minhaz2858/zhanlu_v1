# Enterprise Neutralization — Design Spec

- Date: 2026-10-08
- Repo: `/Users/minhazulislam/Desktop/zhanlu_n8n_main_version`
- Base commit: `2dbc5a6a0`
- Status: approved (design gate), implementation not started

## 1. Problem

This checkout is a Zhanlu enterprise-AI platform that has accumulated content specific to
one customer (Ecisco / EDIA) and one vertical (petrochemical: C5/C9 resin, raffinate, LZ
price tables) plus its bespoke artifacts (dashboards, migrations, tests, scripts, docs).
The platform must be **industry-agnostic**: no trace of the specific enterprise or its
vertical, with an automated guard so it cannot creep back.

The vendored third-party trees checked into this repo (`NL2SQL/DB-GPT-main`,
`n8n-master`, `deepseek-harness-master`, `superpowers-main`) are **out of scope** — they
are reference code, not ours.

## 2. Goal / Non-goals

**Goal**
- Zero enterprise/vertical tokens in our own source, enforced by a checker.
- Forecasting engine + dashboard framework **kept** and made domain-agnostic
  (capability retained, petrochemical bindings removed).
- Memory and skills scrubbed.

**Non-goals**
- Do not touch vendored trees.
- Do not redesign the forecasting or dashboard architecture beyond removing the binding.
- Do not touch unrelated in-flight work (the working tree currently carries 9 unrelated
  modified/untracked files that must not be swept into any commit here).

## 3. Decisions log

| # | Decision | Choice |
|---|----------|--------|
| Q1 | How aggressive | **A — FULL**: delete enterprise files, strip hardcoded references from surviving code, scrub memory+skills, enforce with a checker |
| Q2 | Boundary of "the enterprise" | **B — WIDE**: company + products + vertical framing all go; no petrochemical sample retained |
| Q3 | Fate of vertical features | **B — GENERICIZE the engines**: keep a domain-agnostic forecasting service + dashboard framework; delete the named dashboards and C5/C9 price-table bindings |
| — | Execution approach | **1 — GATE-FIRST**: denylist + automated checker first, then refactor until green |
| D1 | `aipdp_data_warehouse_prod` KB row + credentials | **delete** (copy stack `:5401`) |
| D2 | alembic 036/037/042 | **delete + repair** `down_revision` chain |
| D3 | Stray dirs `ecisco_dw_catalog/`, `.codebuddy/plans/*ecisco*`, `agent_qa/` | delete the first two; `agent_qa/` has **0** token hits → **leave alone** |

## 4. Definitions

### 4.1 Scan scope (own source only)
`backend/app`, `backend/tests`, `backend/alembic`, `backend/skills`, `frontend/src`,
`scripts`, `docs`, `Makefile`, `README.md`, `PROJECT.md`, `ZHANLU_PROJECT_OVERVIEW.md`.

Excluded paths: `NL2SQL/`, `n8n-master/`, `deepseek-harness-master/`, `superpowers-main/`,
`node_modules/`, `backups/`, `data/`, `uploads/`, `agent_qa/`, `.git/`, and all binaries
(the checker must read text only — see §8 pitfall P1).

### 4.2 Denylist tokens (word-boundary, case-insensitive)
```
ecisco  ecisco_bi  EDIA  edia_mysql  c5c9  c5_c9  C5_RESIN  raffinate
md_t_lz_price  lz_v_  petrochemical  aipdp  erp_v_sale_orderentry
惠州  伊斯科  异戊二烯  戊烷
```
Plus the four dashboard slugs (`c5c9-daily-sales`, `c5c9-live-market-reference`,
`sales-perf-dashboard-c5c9`, `sales-performance-c5c9`).

An explicit **allowlist** file exists for justified exceptions; the target end state is an
**empty** allowlist.

**Context-dependent tokens** (checked per-occurrence, not auto-failed): `FAMOUNT`,
`FALLAMOUNT`, `erp_v_sale_orderentry`, `org_name`, `material_name`. These are borderline —
they read as generic ERP column names, but in this repo they are the enterprise's actual
schema. Rule: allowed **only** inside provider/config code that is itself domain-agnostic
(a driver normalising whatever columns the bound datasource exposes); any hardcoded
occurrence tied to the one warehouse is deleted. Each surviving occurrence must be listed
in the allowlist file with a one-line justification.

### 4.3 Measured footprint (own source, word-matched)
| token | files |  | token | files |
|---|---|---|---|---|
| ecisco | 104 | | c5c9 | 31 |
| edia/EDIA | 61 | | md_t_lz_price | 27 |
| ecisco_bi | 5 | | C5_RESIN | 27 |
| petrochemical | 25 | | lz_v_ | 14 |
| raffinate | 8 | | | |

By area: `backend/tests` 33, `docs/superpowers/plans` 16, `scripts` 12,
`docs/superpowers/specs` 9, `backend/alembic/versions` 4, `backend/app` ~40.

## 5. Work breakdown

### S1 — Deletions (hard delete, no tombstones)
- **Dashboards (7 apps, ~40 files)**: `c5_c9_financial_overview`, `c5c9_daily_sales`,
  `c5c9_live_market_reference`, `sales_perf_dashboard_c5c9`, `sales_performance_c5c9`,
  `sales_performance_dashboard_5`, `ceo_demo_001`.
  **Keep** `_template` (generic base) and the 5 clean apps: `ceo_decision_center_demo`,
  `daily_sales_data_sync`, `editorial_demo_001`, `sales_performance_dashboard`,
  `sales_performance_dashboard_pro`.
- **alembic**: `036_rename_ecisco_bi_agent.py`,
  `037_ensure_12_ecisco_forecast_targets.py`, `042_ecisco_bi_silent_agent.py` — delete and
  re-point `down_revision` so the chain stays linear/single-head.
- **Tests**: ~50 of the 55 token-hitting test files. Criterion: **delete** a test whose
  subject is an enterprise-only feature (ecisco agent, C5/C9 dashboard, EDIA routing);
  **genericize** a test that merely uses an enterprise name as a fixture value while
  exercising generic behaviour.
- **Scripts**: `e2e_c5c9_ui.py`, `migrate_erp_datasources.py`, `probe_warehouse_deep.py`,
  `probe_warehouse_tier3.py`; the forecast ops scripts (`backdate_forecast_runs`,
  `backfill_forecast_runs`, `backfill_upstream_forecast`, `seed_and_run_upstream_forecast`,
  `verify_forecast_pipeline`, `test_forecast_run2`) are regenerated generically in S2
  rather than deleted blind. (`e2e_ceo_dynamic.py`, `e2e_ceo_ui.py`,
  `e2e_contract_docx.py`, `e2e_qwen_docx.py`, `run_copy_stack.sh` matched only on
  context-dependent tokens and are **kept** unless the checker flags a real hit.)
- **Docs**: ecisco/edia `docs/superpowers/{plans,specs}`, `docs/plans/ecisco*`,
  `docs/archive/root-scratch/EDIA_Sales_Report_2026-07.html`,
  `docs/archive/root-scratch/live_editable_c5c9.pptx`.
- **Other**: `.codebuddy/plans/ecisco-*`, top-level `ecisco_dw_catalog/`.

### S2 — Genericization (keep engine, drop binding) — ~28 `backend/app` files
- `services/forecasting/`: `mysql_data_source.py`, `datasource_registry.py`,
  `features/exogenous_loaders.py`, `features/warehouse_loaders.py`,
  `finetuning/dataset_builder.py`, `finetuning/finetune_runner.py`,
  `ops/realization_spread.py`, `accuracy_tracker.py`, `domain_signals_ab.py`,
  `domain_signal_calibration.py`, `domain_signals.py`, `preprocess.py`
  → replace hardcoded product/table names with a **config-driven target-series + signal
  registry**; no petrochemical identifiers remain.
- `services/`: `agent_prompts.py`, `system_agents.py`, `response_cache.py`,
  `skill_routing/post_router_hook.py`, `deep_research/{llm_backend,orchestrator}.py`,
  `db/join_partner.py`, `tool_handlers/{dashboard_tools,forecast_tool,web_search_tool}.py`
- `routers/agents.py` (comments and any enterprise branches)
- `domain_configs/default.json` — the neutral default lives here
- `dashboards/_template/frontend/src/{index.css,panels/index.jsx,widgets/index.jsx}`
- `frontend/src/lib/__tests__/runAutomationTask.test.jsx` (only real frontend hit)
- `backend/skills/weekly-report-generation`

### S3 — Data / DB (copy stack `:5401`)
- `agent_apps` (17 rows): verified **0** enterprise references.
- `knowledge_bases` (11 rows): all generic except `aipdp_data_warehouse_prod` → delete
  row + stored credentials (D1).
- `projects`, `forecast_targets`, orgs: clean.

### S4 — Memory + skills
- `MEMORY.md`: drop the ERP-Ecisco, aipdp-DW and "not neutral" entries.
- `USER.md`: drop the "Project: Ecisco (NOT NovaChem)" line.
- Skills: delete `ecisco-dw-catalog`, `ecisco-daily-dashboard`; rewrite
  `zhanlu-forecasting` + its ecisco reference generically; de-brand
  `petrochemical-dashboard-design`.

### S5 — The gate
- `scripts/enterprise_token_denylist.txt`
- `scripts/check_no_enterprise_tokens.py` — text-only scan of §4.1, word-boundary match
  against §4.2, honours the allowlist, exits 1 on any hit with file:line output.
- `Makefile` target `check-tokens`, wired into the existing lint target, plus a
  pre-commit hook.

## 6. Verification (definition of done)

1. `make check-tokens` exit 0 (allowlist empty).
2. Backend imports clean and boots; health endpoint returns 200.
3. `pytest` green.
4. Dashboard registry lists only neutral apps.
5. `alembic heads` → exactly one head.
6. `npm run build` (frontend) green.

## 7. Execution order

1. Land S5 (gate + denylist) as its own commit — the guard exists before the surgery.
2. S1 deletions (dashboards → alembic+repair → tests → scripts → docs → strays).
3. S2 genericization, driven by the checker.
4. S3 DB, S4 memory/skills.
5. Run §6 end-to-end on the **final** tree.

## 8. Pitfalls

- **P1 — binary/`media` false positives.** `git grep -E '\b...\b'` silently matches
  nothing (POSIX ERE has no `\b`), and naive substring matching yields ~5,000 bogus
  "EDIA" hits from the word `media` in bundles/fonts. The checker must be text-only and
  word-boundary aware (ripgrep `-w`).
- **P2 — vendored trees.** `NL2SQL/`, `n8n-master/`, `deepseek-harness-master/`,
  `superpowers-main/` dominate the repo by file count and must be excluded, or the
  "footprint" is meaningless.
- **P3 — unrelated dirty tree.** Do not commit or revert the 9 pre-existing dirty files.
- **P4 — migration chain.** Deleting 036/037/042 without repairing `down_revision`
  breaks `alembic upgrade head`.
- **P5 — generated dashboards are marked `DO NOT EDIT`.** They are generated artifacts;
  delete the app dirs rather than hand-editing, and clear any generated registry/cache
  that references the deleted slugs.
- **P6 — do not strand references.** Deleting a dashboard/datasource leaves dangling
  slugs/ids in registries, caches, `response_cache`, or seeded automation rows; grep for
  each deleted slug after deletion.
- **P7 — "non-empty ≠ answered".** Verification must run on the final source tree, never
  on an earlier green run.

## 9. Assumptions + explicit non-actions

**Verified, not assumed** (checked 2026-10-08 on base `2dbc5a6a0`):
- The 5 "kept" dashboards (`ceo_decision_center_demo`, `daily_sales_data_sync`,
  `editorial_demo_001`, `sales_performance_dashboard`, `sales_performance_dashboard_pro`)
  measure **0** enterprise-token hits. `_template` measures 4 occurrences across 3 files.

**Explicitly NOT done** (recorded so absence is a decision, not an oversight):
- Vendored trees left untouched: `NL2SQL/DB-GPT-main` (25,429 tracked files),
  `n8n-master` (27,613), `deepseek-harness-master` (12,071), `superpowers-main` (172).
- `agent_qa/` (71 files) — 0 token hits, generic QA harness → untouched.
- `M24x2x75_Bolt_Technical_Drawing.md` (top-level) — a stray Fusion 360 bolt artifact,
  carries no enterprise/vertical token. Out of scope; left as-is. Flagged only so it can
  be swept separately if unwanted.
- `ohmo/`, `node_modules/`, `uploads/` — 0 tracked files.
- Generic business terminology stays: "ERP", "sales", "customer", "product",
  "warehouse" are platform vocabulary, not enterprise identity.
- No dashboard is *re-skinned* to a synthetic sample: under Q2=B the named dashboards are
  deleted, not renamed. The dashboard framework + `_template` remain as the neutral base.

