# Forecast MLOps + Human-in-the-Loop — Self-Learning & Drift Auto-Response

**Date:** 2026-08-05
**Status:** Confirmed by user (combined spec, validated bias-correction, auto-adjust+notify, Decision Board card, Approach A in-process scheduler)
**Trigger:** Two requests — (1) AI DevOps/MLOps: daily self-retraining, drift detection, seamless deployment of improved models; (2) Human-in-the-Loop: when AI forecasts a price the user disagrees with (e.g. 异戊二烯 10,388 → user believes 11,000), the user flags it, explains why, and the agent learns from validated feedback.

## Problem

The forecasting engine has the *scaffolding* for self-learning but the loop is open — the keystone field that closes it is never written:

1. **`realized_mape` is never populated.** `ForecastAccuracyLog.realized_mape` (model line 140) and `evaluated_at` (line 141) exist, but no code path writes them. The backtest job writes only the *in-sample* `mape`/`naive_mape`/`per_model` — never the *out-of-sample* realized error once actuals arrive.
2. **Drift detection is a silent no-op.** `accuracy_tracker.detect_drift()` (lines 88-133) filters on `realized_mape.isnot(None)` → always returns `{"is_drifting": False, "reason": "insufficient data"}`. The 20% drift threshold has never fired.
3. **Adaptive weights have nothing to adapt to.** `adaptive_weights()` (lines 140-161) falls back to `backtest_weights` whenever `realized_weights is None` — which is always, because realized per-model accuracy is never recorded.
4. **No human feedback channel.** A user who knows the AI price is wrong has no way to record the correction + reasoning. There is no `forecast_feedback` table, no override capture, no learning from analyst judgement.
5. **No MLOps closed loop.** Models already refit from scratch on every nightly run (8-model ensemble in `engine.compute_target_anchored`), so there is no stale-artifact / model-registry problem — but there is no *evaluation* step to score past runs against newly-arrived actuals, and no automated response when accuracy degrades.

## Design Decisions (user-confirmed)

| Question | Decision |
|---|---|
| Spec scope | **Combined** — MLOps self-learning + HITL in one spec (they share the evaluation/actuals backbone) |
| HITL learning mode | **Validated bias-correction** — overrides scored against later actuals; only trusted authors shift the forecast; raw opinions never move the needle |
| Drift response | **Auto-adjust + notify** — weight shift on drift + dashboard chip + brief disclosure (not just silent logging) |
| Correction UX | **Decision Board card** — "修正预测" modal on the existing board, not a new page |
| Architecture | **Approach A — in-process scheduler.** Extend the existing `_nightly_forecast_loop` (2 AM UTC) in `scheduled_tasks.py`. No APScheduler, no new infra, no new dependencies. |
| Model registry (Approach C) | **Rejected as YAGNI** — ensemble refits from scratch per run; no stale-artifact problem to solve |

## Architecture

The closed loop, reusing the existing 2 AM UTC nightly cycle:

```
Nightly cycle (scheduled_tasks._nightly_forecast_loop, 2 AM UTC)
  │
  ├─ [existing] seed targets → compute_target_anchored per product (8-model refit)
  │
  ├─ [NEW] evaluation_job   ── match past forecasts + overrides → newly-arrived actuals
  │                            → fill realized_mape / evaluated_at on ForecastAccuracyLog
  │                            → score pending forecast_feedback rows (beat/miss)
  │
  ├─ [NEW] drift_response    ── detect_drift() now has data → on drift: shift adaptive weights,
  │                            persist audit to forecast_weight_adjustments, set dashboard chip,
  │                            flag analyst brief to disclose the adjustment
  │
  └─ [NEW] bias_correction   ── trust-gated: if an author's scored overrides meet the gate,
                                compute validated bias delta → blend into published forecast
                                (±10% cap), disclose in brief. Skipped until trust earned.

Human-in-the-Loop (user-initiated, any time):
  Decision Board card → "修正预测" modal
    → POST /api/market-dashboard/forecast-feedback {product_id, ai_price, user_price, reason}
    → forecast_feedback row (status=pending) — does NOT move the forecast yet
    → scored later by evaluation_job once the target date's actual arrives
```

**Honesty boundary (preserved):** Decision-engine thresholds (`p_rise ≥ 0.70`, buy/hold/sell/watch gate) remain 100% deterministic. Bias correction only nudges the *published price*, never the action call, and the analyst brief discloses when it was applied. One wrong user opinion can never poison a forecast — it must first beat the AI against reality, repeatedly.

## Section 1 — Evaluation job (closes the realized_mape loop)

**New file:** `backend/app/services/forecasting/ops/evaluation_job.py`

The keystone. Without this, drift detection and adaptive weights are dead code.

- **Input:** for each `ForecastTarget`, find `ForecastRun` rows whose `as_of_date` + `horizon_days` has now passed AND whose matching actual has arrived in the EDIA MySQL source (`md_t_lz_price` / `lz_v_{product}_data`, via existing `edia_source.py` loaders).
- **Match:** align forecast horizon dates to actual dates; reuse `accuracy_tracker.compute_realized_error()` (already implemented, lines 21-39) which returns `{mape, n_matched}`.
- **Write:** update the corresponding `ForecastAccuracyLog` row's `realized_mape`, `realized_error`, `evaluated_at = now()`. Per-model realized accuracy (where reconstructable from `model_detail`) feeds a realized-weights map for `adaptive_weights()`.
- **Score HITL:** for each `forecast_feedback` row in `pending` status whose target date has actuals: compute `ai_error = |ai_price - actual|/actual`, `user_error = |user_price - actual|/actual`; set `beat = user_error < ai_error`, `status = scored`, persist errors. This is the evidence the trust gate consumes.
- **Idempotent:** re-running only scores unscored/pending rows; never overwrites a scored row.
- **Gated by** `FORECAST_EVAL_JOB_ENABLED` (default off). Invoked as a new step inside `_run_nightly_forecast_sync` after the compute loop, plus exposed via `POST /api/forecast-ops/run-evaluation` for manual backfill.

## Section 2 — Drift auto-adjust + notify

**New file:** `backend/app/services/forecasting/ops/drift_response.py`

Now that `detect_drift()` returns real data:

- **On drift detected** (`is_drifting=True`): call `adaptive_weights()` with the realized-weights map from Section 1 to produce shifted weights. The next nightly compute cycle picks these up as the blend base (engine already accepts a weights override path).
- **Audit:** persist a `forecast_weight_adjustments` row: `{target_id, triggered_by: "drift", baseline_mape, recent_mape, old_weights, new_weights, created_at}`. Every automated weight change is traceable.
- **Notify (two surfaces):**
  - Dashboard chip — a `forecast_ops_status` cache (product → `{drifting: bool, last_adjusted_at, delta}`) read by the Decision Board header.
  - Analyst brief disclosure — when bias/drift adjustment was applied to a product's published forecast, `evidence_pack` includes a `bias_note` so the LLM/template brief states "本预测已根据近期实际偏差自动校正" rather than presenting the adjusted number as raw model output.
- **Gated by** `FORECAST_DRIFT_AUTO_ADJUST_ENABLED` (default off). Detection (read-only) can run with just `FORECAST_EVAL_JOB_ENABLED`; the *adjustment* is the separately-gated write.

## Section 3 — HITL feedback capture

**New file:** `backend/app/services/forecasting/ops/feedback_service.py`

- **Capture:** `record_feedback(product_id, ai_price, user_price, reason, author)` → inserts `forecast_feedback` row, `status=pending`. Captures the AI price at feedback time so scoring is stable even if the forecast later changes.
- **Does NOT move the forecast.** A pending override is an assertion, not an action. The forecast stays as the AI published it until the trust gate is earned (Section 4).
- **Author track-record:** the service aggregates an author's scored overrides (count, beat-rate, mean signed delta) to feed the trust gate.
- **List:** `list_feedback(product_id)` returns the product's feedback history with scores, for the modal's history view.
- **Gated by** `FORECAST_HITL_ENABLED` (default off). Capture can be on while bias-correction is still off (useful for collecting scoring evidence before enabling learning).

## Section 4 — Validated bias-correction learning

**New file:** `backend/app/services/forecasting/ops/bias_correction.py`

The trust-gated learning layer. An author's judgement only shifts a forecast after it has *demonstrably beaten the AI against reality*.

**Trust gate (per author, per product):**
- **≥3 scored overrides** — one or two calls can be luck.
- **>50% beat-rate** — author must be right more often than the AI on the same products.
- **30-day recency half-life** — old overrides decay; weight ∝ 0.5^(age_days/30). A once-good analyst who goes cold stops influencing.
- **±10% cap** — even a trusted author can only nudge the published forecast by ±10% from the model output. Prevents a runaway correction.

**Computation:** for a product whose author passes the gate, compute the validated mean signed delta (user_price − ai_price) over recent scored overrides, weighted by recency. Blend: `published = model_output * (1 + clamp(mean_delta_ratio, -0.10, +0.10))`. If the author does not pass the gate, `published = model_output` unchanged.

**Integration point:** `engine.compute_target_anchored` → guard/publish step (`guard.py`). The bias delta applies to `guard_result.published_forecast` only, after the deterministic guard. Decision-engine action (buy/hold/sell/watch) is computed from the *pre-bias* model output — bias changes the number, never the call.

**Disclosure:** when applied, `explanation["bias_correction"] = {author, delta_ratio, n_overrides, beat_rate, clamped: bool}` so the analyst brief and audit trail show it.

**Gated by** `FORECAST_BIAS_CORRECTION_ENABLED` (default off). Requires `FORECAST_EVAL_JOB_ENABLED` (need scored overrides) and `FORECAST_HITL_ENABLED` (need captures).

## Section 5 — Decision Board card UX

On the existing Decision Board (`components/DecisionBoard.jsx`), each product card gains a "修正预测" action:

- **Modal fields:** AI 预测价 (read-only, pre-filled from current published forecast), 我认为应该是 (user price input), 理由 (textarea), 提交.
- **Validation:** user price must be a positive number within a sane band (e.g. 0.1×–10× AI price) to reject typos.
- **History view:** below the form, list the product's past feedback rows: date, AI price, user price, actual (once scored), beat/miss badge, reason.
- **Color rule (existing convention):** 涨=red / 跌=green (Chinese market colors, already used on the board). Beat badge = neutral positive; miss = muted.
- **authFetch:** reuses the existing `authFetch`-based API pattern from the board; no new auth surface.

## Section 6 — API endpoints

| Method | Path | Purpose | Gate |
|---|---|---|---|
| POST | `/api/market-dashboard/forecast-feedback` | Submit a correction | `FORECAST_HITL_ENABLED` |
| GET | `/api/market-dashboard/forecast-feedback?product_id=` | List feedback history for a product | `FORECAST_HITL_ENABLED` |
| GET | `/api/forecast-ops/status` | Drift/adjustment status chips + eval job last-run | `FORECAST_EVAL_JOB_ENABLED` |
| POST | `/api/forecast-ops/run-evaluation` | Manually trigger evaluation job (backfill) | `FORECAST_EVAL_JOB_ENABLED` + admin |

Routers: feedback endpoints extend `backend/app/routers/market_dashboard.py`; ops endpoints in a small new `backend/app/routers/forecast_ops.py`. Both reuse `require_admin` dep where appropriate.

## Section 7 — Data model

**New table `forecast_feedback`** (migration, extends Alembic head `040_report_session_project_agent`):

| Column | Type | Notes |
|---|---|---|
| id | String(36) PK | UUID |
| target_id | String(36) FK → forecast_targets.id | indexed |
| product_id | String | denormalized for convenience |
| ai_price | Float | AI published price at feedback time |
| user_price | Float | user's asserted price |
| reason | Text | why the user disagrees |
| author_id | String(36) | user id (for trust gate track-record) |
| author_name | String | denormalized for display |
| target_date | DateTime | the forecast date being corrected |
| status | String(20) | `pending` → `scored` |
| ai_error | Float nullable | filled at scoring |
| user_error | Float nullable | filled at scoring |
| beat | Bool nullable | user_error < ai_error |
| scored_at | DateTime nullable | |
| created_at / updated_at | DateTime | TimestampedBase |

**New table `forecast_weight_adjustments`** (audit):

| Column | Type | Notes |
|---|---|---|
| id | String(36) PK | UUID |
| target_id | String(36) FK → forecast_targets.id | indexed |
| triggered_by | String(20) | `drift` \| `bias_correction` |
| reason | Text | e.g. "recent=18.3 vs baseline=12.1" |
| old_weights | JSON | |
| new_weights | JSON | |
| delta_ratio | Float nullable | bias delta if applicable |
| created_at | DateTime | |

`realized_mape` / `evaluated_at` on the existing `ForecastAccuracyLog` are reused as-is (no schema change — the field already exists, it was just never written).

## Section 8 — Feature flags & rollout

All flags default **OFF** in `.env`. Order matters — each step depends on the prior:

| Flag | Purpose | Depends on |
|---|---|---|
| `FORECAST_EVAL_JOB_ENABLED` | Run evaluation job nightly + manual | — |
| `FORECAST_DRIFT_AUTO_ADJUST_ENABLED` | Auto weight-shift on drift | eval job |
| `FORECAST_HITL_ENABLED` | Capture feedback (modal active) | — |
| `FORECAST_BIAS_CORRECTION_ENABLED` | Trust-gated bias nudge | eval job + HITL |

**Rollout sequence:**
1. Deploy code + migration (`forecast_feedback`, `forecast_weight_adjustments`) → docker `--force-recreate backend` (env_file baked at creation; **remember: plain restart won't pick up new .env flags**). Reinstall pandas/statsmodels/xgboost in container if image is stale (known gotcha).
2. `FORECAST_EVAL_JOB_ENABLED=true` → run `POST /api/forecast-ops/run-evaluation` to backfill `realized_mape` for existing ForecastRun history.
3. Verify `realized_mape` populated + `detect_drift()` returns real results (not "insufficient data").
4. `FORECAST_DRIFT_AUTO_ADJUST_ENABLED=true` → verify `forecast_weight_adjustments` rows appear on drift, chips render.
5. `FORECAST_HITL_ENABLED=true` → submit test corrections, verify `forecast_feedback` rows land as `pending`.
6. Wait for actuals → evaluation job scores them → verify `beat`/`miss`.
7. `FORECAST_BIAS_CORRECTION_ENABLED=true` only after ≥3 overrides per author are scored — verify the gate holds and `explanation["bias_correction"]` appears.

## Section 9 — Testing

- **evaluation_job:** unit test with seeded `ForecastRun` + mock actuals → asserts `realized_mape` written, idempotency (re-run no-ops), HITL scoring (beat/miss). Contract test on the MySQL actuals match (mock `edia_source` loader).
- **drift_response:** unit test that a drifted product produces a `forecast_weight_adjustments` row and shifted weights; non-drifted product no-ops.
- **bias_correction:** unit test the trust gate directly — author with 2 overrides (below gate) → no shift; 3 overrides 66% beat-rate → shift within ±10% cap; cap clamping; recency decay.
- **feedback_service:** unit test capture + list + author track-record aggregation.
- **API:** contract tests for the 4 endpoints (auth, validation band, gate-off → 404/501).
- **Frontend:** vitest for the modal (validation, history rendering) following existing `DecisionBoard` test pattern.
- **E2E (manual):** submit a correction → wait/force-score → verify brief discloses bias correction.

Run frontend tests individually (happy-dom, 4+ files timeout per known constraint). No pytest in venv — backend tests run via the project's existing test harness.

## Out of scope

- **Model registry / artifact versioning (Approach C).** Rejected as YAGNI — ensemble refits from scratch per nightly run; there is no persistent artifact to version.
- **Changing decision-engine thresholds or the buy/hold/sell/watch gate.** Bias correction nudges the price, never the action.
- **Real-time (request-time) retraining.** Learning happens nightly, not on every page load.
- **Multi-author disagreement resolution.** Trust gate is per-author; if two trusted authors disagree, their deltas blend by recency-weighted mean. No voting/quorum logic.
- **New data ingestion pipelines.** Evaluation reuses existing `edia_source.py` MySQL loaders.
