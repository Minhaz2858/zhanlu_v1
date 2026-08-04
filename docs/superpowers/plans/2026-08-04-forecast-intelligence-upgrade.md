# Forecast Intelligence Upgrade — Phase 4 to Phase 7 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the nightly forecast intelligence pipeline with (a) policy-driven bias correction + volatility scaling on every published forecast, (b) automatic quality-gated approval of high-confidence intelligence events, (c) per-product overlay metadata (intelligence + policy) surfaced in the Market Dashboard UI as badges, and (d) integration tests + a clean deployment of all four changes.

**Architecture:** Four orthogonal modules added to the existing forecast intelligence pipeline. Each phase is independently gated, gracefully degrades on failure, and is observable through both structured logs and the dashboard UI.

```
                         ┌──────────────────────────────────────────┐
                         │ ForecastEngine.compute_target_anchored() │
                         └────────────────┬─────────────────────────┘
                                          │
              ┌───────────────────────────┼─────────────────────────────┐
              ▼                           ▼                            ▼
   ┌────────────────────┐    ┌────────────────────────┐    ┌──────────────────────────┐
   │ Step 8.5: Intel    │    │ Step 8.6: Policy       │    │ Step 9: Explanation      │
   │ Overlay            │───▶│ Service (NEW Phase 4)  │───▶│ + drift + overlay_meta   │
   │ (Phase 3, done)    │    │ bias + vol scaling     │    │ (extended Phase 6)       │
   └────────────────────┘    └────────────────────────┘    └────────────┬─────────────┘
                                                                        │
                                                                        ▼
   ┌──────────────────────────────────────────────────────────────────────────────────────┐
   │ ForecastRun.results[horizon].explanation = {                                         │
   │     "intelligence": {...},                                                            │
   │     "policy": {...},           ← Phase 4                                              │
   │     "drivers": [...], "coherence_flags": [...],                                       │
   │     "drift_warning": "...",                                                           │
   │ }                                                                                     │
   └──────────────────────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
   ┌──────────────────────────────────────────────────────────────────────────────────────┐
   │ MarketDashboard API                                                                  │
   │  • /forecast-chart/{pid}            → ForecastChartResponse.overlay                  │
   │  • /forecast-base-batch             → ForecastBaseBatchResponse.overlay_by_product   │
   │  • /overview                        → OverviewBundle.forecast_overlay_by_product     │
   └──────────────────────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
   ┌──────────────────────────────────────────────────────────────────────────────────────┐
   │ Frontend (Phase 6)                                                                    │
   │  • ForecastCard.jsx           → volatility badge (Activity) + intel badge (Zap)       │
   │  • ForecastTrendChart.jsx     → overlay badges in chart header                       │
   │  • OverviewDashboard.jsx      → inline overlay indicators in product rows             │
   └──────────────────────────────────────────────────────────────────────────────────────┘

   ┌──────────────────────────────────────────────────────────────────────────────────────┐
   │ IntelligenceEventRegistry.upsert_event()    ← Phase 5: auto-approval logic           │
   │   impact_magnitude ≥ 0.7 AND relevance_score ≥ 0.7 AND credibility ∈ {tier1,tier2}    │
   │   → review_status = "auto_approved"                                                  │
   └──────────────────────────────────────────────────────────────────────────────────────┘
```

**Tech Stack:** Python 3.11, SQLAlchemy 2.0, pandas, FastAPI, Pydantic, React/Vite/Tailwind, lucide-icons. Backend runs in Docker (no `--reload` — `docker compose restart backend` after service changes).

**Spec:** This document IS the spec + plan combined (Phase 4-7 is small enough to fit in one doc; previously documented at `docs/superpowers/plans/2026-08-03-forecast-enhancement-phase1.md` for Phases 1-3).

---

## Global Constraints

- **Project root:** `/home/ysk2025/zhanlu_7_30` (NOT `/home/ysk2025/zhanlu` or `zhanlu-deploy`).
- **Backend venv:** `backend/venv/bin/python` — use this for all test runs. pytest is NOT in requirements.txt but IS installed in the venv.
- **Test runner:** `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/<file>::<test> -v` — run individually, SSH RAM is limited (no parallelism, no backgrounding).
- **Docker:** Backend has no `--reload`. After service code changes: `docker compose restart backend`.
- **Frontend:** Bind-mounted to nginx. After component changes: `cd /home/ysk2025/zhanlu_7_30/frontend && npm run build` (auto-rebuilds on save too).
- **Alembic:** Latest migration is `039_forecast_enhancement_phase1.py`. No new migration needed for Phases 4-7.
- **Code style:** Follow existing patterns — `from __future__ import annotations`, dataclasses for result types, `TimestampedBase` for DB models, lazy imports for heavy deps, type hints everywhere.
- **Honesty gate is sacred:** Never bypass `guard.py`. The gate catching downstream products means the cascade error dominated — that's correct self-correcting behavior, not a bug to fix.

---

## File Structure

**Modified files:**

| File | Change |
|---|---|
| `backend/app/services/forecasting/engine.py` | Add Step 8.6 — policy service bias + vol scaling |
| `backend/app/services/intelligence/event_registry.py` | Add `_compute_auto_approval()` + wire into `upsert_event()` + bucket in `review_summary()` |
| `backend/app/schemas/market_dashboard.py` | Add `ForecastOverlayMeta`, `IntelligenceOverlayMeta`, `PolicyOverlayMeta`; extend `ForecastBaseBatchResponse`, `ForecastChartResponse`, `OverviewBundle` |
| `backend/app/services/market_dashboard_service.py` | Add `_build_overlay_meta()` helper; populate `overlay_by_product` in `get_forecast_base_batch_v2`, `overlay` in `get_forecast_chart`; switch `get_overview_bundle` to v2 |
| `backend/app/config.py` | Add `FORECAST_POLICY_ENABLED` flag |
| `frontend/src/pages/MarketDashboard/components/ForecastCard.jsx` | Add volatility + intel badges per product |
| `frontend/src/pages/MarketDashboard/components/ForecastTrendChart.jsx` | Add overlay badges in chart header |
| `frontend/src/pages/MarketDashboard/components/OverviewDashboard.jsx` | Add inline overlay indicators in product rows |
| `backend/tests/conftest.py` | Fix SQLite in-memory sharing via StaticPool |

**New test files:**

| File | Tests |
|---|---|
| `backend/tests/test_intelligence_event_auto_approval.py` | Phase 5: 15 tests covering auto-approval rules + edge cases |
| `backend/tests/test_phase4_7_integration.py` | Phase 4-7: 18 tests covering wiring + schemas + smoke |

**No DB migration needed** — the new columns (`policy`, `intelligence` in explanation dict) are JSON inside `results[horizon].explanation`, no schema change.

---

## Task 1: Phase 4 — Wire forecast_policy_service into engine.py

**Files:**
- Modify: `backend/app/services/forecasting/engine.py`
- Test: `backend/tests/test_forecast_policy_service.py` (already exists, 13 tests pass)

**Goal:** After the intelligence overlay (Step 8.5) and before the explanation block (Step 9), call `ForecastPolicyService.compute_from_accuracy_log()` to compute policy metrics, then apply the combined bias + volatility correction factor to the published forecast. Store the policy detail in `explanation_dict["policy"]` so the dashboard can surface it.

### Step 1.1: Verify the policy service exists and is testable

- [x] Run `venv/bin/python -m pytest tests/test_forecast_policy_service.py -v` → **13/13 PASS** (pre-existing).
- [x] Verify import: `from app.services.forecasting.forecast_policy_service import ForecastPolicyService, ForecastPolicyMetrics, VolatilityRegime` works.

### Step 1.2: Insert Step 8.6 in `compute_target_anchored()`

- [x] Locate the intelligence overlay block in `engine.py` (after `intelligence_report = None` assignment).
- [x] Add Step 8.6 directly after — guards with `_get_config_bool("FORECAST_POLICY_ENABLED", True)`.
- [x] Build daily log returns: `np.log(y / y.shift(1)).dropna().tolist()` (only if `len(y) > 1`).
- [x] Call `ForecastPolicyService.compute_from_accuracy_log(db=..., product_key=..., org_id=..., daily_returns=daily_returns)`.
- [x] Compute combined factor: `(1.0 + (bias_pct + diagnosis_bias) / 100.0) * vol_multiplier`.
- [x] Apply: `guard_result.published_forecast = guard_result.published_forecast * factor`.
- [x] Log: `[policy] %s: bias=%.3f%%, vol=%s(mult=%.2f), factor=×%.4f, n=%d` at INFO level.
- [x] Wrap in `try/except Exception` — log warning + set `policy_report=None` on failure.
- [x] Add `"policy": policy_report` to `explanation_dict` in Step 9.

**Code location:** `engine.py` lines 380-435 (the policy block) + line 478 (`explanation_dict["policy"]`).

### Step 1.3: Verify wiring with integration test

- [x] Run `venv/bin/python -m pytest tests/test_phase4_7_integration.py::TestPolicyServiceWiring -v` → PASS.

---

## Task 2: Phase 5 — Auto-approve Intelligence Events

**Files:**
- Modify: `backend/app/services/intelligence/event_registry.py`
- Test: `backend/tests/test_intelligence_event_auto_approval.py` (new)

**Goal:** Add quality-gated auto-approval to `IntelligenceEventRegistry.upsert_event()`. Events that meet all three thresholds — `impact_magnitude ≥ 0.7` AND `relevance_score ≥ 0.7` AND `source_credibility ∈ {tier1, tier2}` — are promoted to `review_status = "auto_approved"`. Already-decided statuses (`auto_approved`, `rejected`) are never downgraded.

### Step 2.1: Add `_compute_auto_approval()` static method

- [x] Insert after `_default_review_status()` in `IntelligenceEventRegistry`.
- [x] Signature: `(event: ExtractedEvent, desired_status: str) -> str`.
- [x] Hard rule: if `desired_status == "rejected"` → return `"rejected"` unchanged.
- [x] Compute `impact = event.impact_magnitude` (derived: `mag_score*0.40 + cert_score*0.35 + rel_score*0.25`).
- [x] Compute `relevance = event.relevance_score` (derived from `relevance_to_c5_c9`).
- [x] Normalize credibility: `(event.source_credibility or "tier2").strip().lower()`.
- [x] Promotion rule: `if impact >= 0.70 and relevance >= 0.70 and credibility in {"tier1", "tier2"}: return "auto_approved"`.
- [x] Default: return `desired_status` unchanged.

### Step 2.2: Wire into `upsert_event()`

- [x] Compute `desired_status = (event.review_status or self._default_review_status()).strip().lower()`.
- [x] Compute `final_status = self._compute_auto_approval(event, desired_status)`.
- [x] Set `"review_status": final_status` in payload (not `desired_status`).
- [x] Log promotion: `[auto-approval] %s → auto_approved (impact=%.2f, relevance=%.2f, credibility=%s)` at INFO level when promoting.

### Step 2.3: Protect existing decided statuses on re-upsert

- [x] In the `if existing:` update loop, before `setattr(existing, key, val)`:
- [x] If `key == "review_status"` and `existing.review_status in ("auto_approved", "rejected")` and `val not in ("auto_approved", "rejected")`:
- [x] `continue` (skip the assignment — never downgrade).
- [x] Log debug: `[auto-approval] preserving existing status=%s for %s (incoming=%s)`.

### Step 2.4: Update `get_review_summary()` bucket

- [x] Add `"auto_approved"` to buckets dict.
- [x] Add `auto_approved_count` to return dict.

### Step 2.5: Write the test file

- [x] Created `backend/tests/test_intelligence_event_auto_approval.py` with 15 tests:
  - `test_high_impact_high_relevance_tier1_approves`
  - `test_high_impact_high_relevance_tier2_approves`
  - `test_low_magnitude_does_not_approve`
  - `test_low_credibility_does_not_approve`
  - `test_unknown_credibility_treated_as_tier2`
  - `test_rejected_event_is_never_auto_approved`
  - `test_already_auto_approved_stays_auto_approved`
  - `test_pending_event_gets_auto_approved_if_qualifies`
  - `test_pending_event_stays_pending_if_not_high_quality`
  - `test_credibility_case_insensitive`
  - `test_credibility_whitespace_tolerated`
  - 4 sanity tests for derived score ranges

### Step 2.6: Verify

- [x] Run `venv/bin/python -m pytest tests/test_intelligence_event_auto_approval.py -v` → **15/15 PASS**.

---

## Task 3: Phase 6 — Dashboard Chart Enhancement

**Files:**
- Modify: `backend/app/schemas/market_dashboard.py`
- Modify: `backend/app/services/market_dashboard_service.py`
- Modify: `frontend/src/pages/MarketDashboard/components/ForecastCard.jsx`
- Modify: `frontend/src/pages/MarketDashboard/components/ForecastTrendChart.jsx`
- Modify: `frontend/src/pages/MarketDashboard/components/OverviewDashboard.jsx`

**Goal:** Surface the intelligence + policy overlay metadata from every ForecastRun in the Market Dashboard UI as colored badges, so operators can immediately see which forecasts are policy-adjusted and which are intelligence-driven.

### Step 3.1: Add Pydantic schemas

- [x] In `schemas/market_dashboard.py`, add three new classes before `ForecastBaseBatchResponse`:
  - `IntelligenceOverlayMeta(active_event_count, bias_direction, impact_tier, confidence, summary)`
  - `PolicyOverlayMeta(volatility_regime, vol_multiplier, bias_pct, diagnosis_bias, applied_factor)`
  - `ForecastOverlayMeta(product_id, intelligence, policy)`
- [x] Add `overlay_by_product: dict[str, Optional[ForecastOverlayMeta]] = Field(default_factory=dict)` to `ForecastBaseBatchResponse`.
- [x] Add `overlay: Optional[ForecastOverlayMeta] = None` to `ForecastChartResponse`.
- [x] Add `forecast_overlay_by_product: dict[str, Optional[ForecastOverlayMeta]] = Field(default_factory=dict)` to `OverviewBundle`.

### Step 3.2: Service layer — `_build_overlay_meta()` helper

- [x] In `services/market_dashboard_service.py`, add `_build_overlay_meta(product_id, horizon_payload)`.
- [x] Read `horizon_payload["explanation"]`, extract `intelligence` and `policy` sub-dicts.
- [x] Build `IntelligenceOverlayMeta` only if `intelligence_raw` is truthy.
- [x] Build `PolicyOverlayMeta` only if `policy_raw` is truthy.
- [x] Return `ForecastOverlayMeta` if at least one sub-meta is non-None, else return None.

### Step 3.3: Populate overlay in `get_forecast_base_batch_v2()`

- [x] Initialize `overlay: dict = {pid: None for pid in product_ids}`.
- [x] For each loaded run, after extracting `base`, call `_build_overlay_meta(pid, horizon_payload)` and store in `overlay[pid]`.
- [x] Return `ForecastBaseBatchResponse(day=day, base_by_product=out, overlay_by_product=overlay)`.

### Step 3.4: Populate overlay in `get_forecast_chart()`

- [x] Initialize `overlay_meta: Optional[ForecastOverlayMeta] = None` before the runs loop.
- [x] After `as_of_date = ...` and `data_quality = "good"`, add `overlay_meta = _build_overlay_meta(product_id, horizon_payload)`.
- [x] Add `overlay=overlay_meta` to the return `ForecastChartResponse(...)`.

### Step 3.5: Use v2 reader in `get_overview_bundle()`

- [x] Replace `forecast_resp = get_forecast_base_batch(pids, day=forecast_day)` with `forecast_resp = get_forecast_base_batch_v2(pids, day=forecast_day)`.
- [x] Add `forecast_overlay_by_product=forecast_resp.overlay_by_product` to `OverviewBundle(...)`.

### Step 3.6: Frontend — ForecastCard.jsx badges

- [x] Add `import { Activity, Zap }` from lucide-icons.
- [x] Read `const overlayByProduct = forecastQuery.data?.overlay_by_product || {};`.
- [x] Add `volBadge(regime)` helper: `'bg-red-100 text-red-700 ...'` for HIGH, `'bg-amber-100 text-amber-700 ...'` for MODERATE, `'bg-emerald-100 text-emerald-700 ...'` for NORMAL.
- [x] Add `biasIcon(direction)` + `biasColor(direction)` helpers.
- [x] After the price row in each product grid item, render badges if `overlayByProduct[pid]` is non-None.

### Step 3.7: Frontend — ForecastTrendChart.jsx badges

- [x] Add `import { Activity, Zap }` from lucide-icons.
- [x] After the "Last refresh" span in the header, render:
  - Volatility badge (red/amber/green palette based on regime).
  - Intelligence badge (purple palette, ↑/↓/— direction + count).

### Step 3.8: Frontend — OverviewDashboard.jsx inline indicators

- [x] Read `const forecastOverlayByProduct = bundle?.forecast_overlay_by_product || {};`.
- [x] After each product's forecast gap line, render a small inline badge row (vol dot + Intel label).

### Step 3.9: Build + verify

- [x] `cd /home/ysk2025/zhanlu_7_30/frontend && npm run build` → succeeds in 35-42s.
- [x] `venv/bin/python -m pytest tests/test_phase4_7_integration.py -v` → **18/18 PASS**.

---

## Task 4: Phase 7 — Fix SQLite test conftest + deploy

**Files:**
- Modify: `backend/tests/conftest.py`

**Goal:** Fix the pre-existing 2 test failures in `test_forecast_tool.py` by switching the in-memory SQLite to a shared connection (StaticPool + `file::memory:?cache=shared`). Then verify the deployment.

### Step 4.1: Diagnose the failure

- [x] Test: `test_target_not_found` and `test_no_targets_no_op` in `tests/test_forecast_tool.py` fail with `sqlite3.OperationalError: no such table: forecast_targets`.
- [x] Root cause: `conftest.py` sets `DATABASE_URL=sqlite:///:memory:` — SQLite `:memory:` is per-connection, so the test fixture creates tables in connection A, then the actual test session opens connection B and sees an empty schema.

### Step 4.2: Fix conftest.py

- [x] Change `os.environ["DATABASE_URL"]` to `sqlite:///file::memory:?cache=shared&uri=true`.
- [x] Add `_ensure_shared_sqlite_engine` session-scope fixture:
  - Detect if the global engine's pool is not `StaticPool`.
  - If so, re-create the engine with `poolclass=StaticPool`.
  - Replace module-level `engine` and `SessionLocal`.
  - Re-register the FK pragma event listener.

### Step 4.3: Run forecast_tool tests

- [x] `venv/bin/python -m pytest tests/test_forecast_tool.py -v` → **36/36 PASS** (was 34/36 before).

### Step 4.4: Deploy

- [x] `cd /home/ysk2025/zhanlu_7_30 && docker compose restart backend` → backend container restarts cleanly.
- [x] `docker ps --filter name=zhanlu-backend --format "{{.Status}}"` → "Up X seconds".

---

## Verification Matrix

| Phase | Tests | Status |
|---|---|---|
| Phase 4 — Policy wiring | `test_forecast_policy_service.py` (13) + `test_phase4_7_integration.py::TestPolicyServiceWiring` (1) | **14/14 PASS** |
| Phase 5 — Auto-approval | `test_intelligence_event_auto_approval.py` (15) | **15/15 PASS** |
| Phase 6 — Dashboard overlay | `test_phase4_7_integration.py::TestForecastOverlayMetaSchema` (4) + `TestPhase6DashboardExtension` (5) | **9/9 PASS** |
| Phase 7 — Fix + deploy | `test_forecast_tool.py` (36) | **36/36 PASS** (was 34/36) |
| Smoke (Phase 7) | `test_phase4_7_integration.py::TestPhase7Smoke` (6) | **6/6 PASS** |
| Intelligence overlay (Phase 3) | `test_intelligence_overlay.py` (17) | **17/17 PASS** (regression check) |
| **TOTAL** | | **97/97 PASS** |

---

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Policy service throws on cold start (no accuracy log rows) | `try/except` wrapper + `policy_report=None` — engine continues with raw forecast |
| Dashboard receives `forecast_overlay_by_product` from a stale `ForecastRun` (no new policy) | `_build_overlay_meta` returns None for missing keys — graceful degradation, badges just don't show |
| Auto-approval promotes low-quality events | Hard rule: requires ALL THREE thresholds (impact + relevance + credibility) |
| Re-ingestion of same event downgrades `auto_approved` to `approved` | Update loop explicitly preserves `auto_approved` and `rejected` |
| Frontend cache holds old schema (no `overlay`) | Cache stores `forecast_base_by_product` only — overlay always fetched fresh from bundle |
| Future Phase 8 may add new overlay fields | `ForecastOverlayMeta` is a Pydantic model — add fields without breaking existing consumers |

---

## Out of Scope (Future Phases)

These are explicitly NOT part of Phase 4-7:

- **Phase 8 — Notification system** — push alerts when `auto_approved` events trigger large forecast swings
- **Phase 9 — Policy backtest UI** — let admins tune bias caps + vol thresholds from the dashboard
- **Phase 10 — Multi-region overlay** — separate intel feeds per region
- **Causal narrative templates** — already exist in `intelligence/narrative.py` but not wired into the UI yet
- **Realized-error feedback loop** — already exists in `accuracy_tracker.py` but only writes logs, no UI

---

## Related Documents

- `docs/superpowers/plans/2026-08-03-forecast-enhancement-phase1.md` — Phases 1-3 (silent scheduler, cache-first, intelligence overlay)
- `docs/superpowers/plans/2026-08-03-nightly-forecast-scheduler.md` — Phase 0 (silent scheduler setup)
- `docs/superpowers/specs/2026-08-03-forecast-enhancement-phase1-design.md` — Design rationale for the 11-stage pipeline
- `backend/app/services/forecasting/forecast_policy_service.py` — Phase 4 module
- `backend/app/services/intelligence/event_registry.py` — Phase 5 module
- `backend/app/schemas/market_dashboard.py` — Phase 6 schemas
- `backend/tests/test_intelligence_event_auto_approval.py` — Phase 5 tests (new)
- `backend/tests/test_phase4_7_integration.py` — Phase 4-7 integration tests (new)