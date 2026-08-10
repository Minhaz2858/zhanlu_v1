# Forecasting Activation & Decision Loop — Parallel Execution Plan

> **Status:** Active (2026-08-06). Three workstreams in parallel, sequenced by ROI.

## Context

14 forecast feature flags are OFF. Months of engineering (Wave 0/1/2/3, MLOps+HITL, foundation models) are dormant. The bottleneck is no longer "build more models" — it's (a) turn on what's built, (b) make the output actionable, (c) get fundamental data.

**Discovery during planning:** Stream A Wave 1/2/3 wiring is **already complete** — `feature_builder.py` accepts `volume_df`, `service.py` loads demand signals, `template_brief.py` renders them, all tests exist. The flags just need turning on with A/B validation.

## Three Workstreams (Parallel)

### Stream A — Activate & Validate (1-2 days, fastest ROI)

**Goal:** Turn on dormant flags one at a time with A/B backtest validation.

**A/B harness:** `backend/tests/walk_forward_backtest.py` (walk-forward, 10 products, h=7/14/30, MAPE+RMSE+bias+dir_acc). No `--compare` flag exists yet — baseline run, then per-flag runs, compare MAPE deltas manually.

**Flag rollout order (by expected ROI):**

| # | Flag | Wave | Expected effect | Risk |
|---|------|------|-----------------|------|
| 1 | `FORECAST_ERP_VOLUME_EXOG_ENABLED` | W1 | ERP sales volume → XGBoost exog. "Biggest ROI data fix" per memory | Low — exog feature, drops to 0 if missing |
| 2 | `FORECAST_DEMAND_SIGNAL_ENABLED` | W1 | Demand/supplier signals → AI Brief 供需研判 section | None — display only, no model impact |
| 3 | `FORECAST_DECISION_LOGGING_ENABLED` | W2 | Log decisions → enables ROI backtest + threshold calibration | None — logging only |
| 4 | `FORECAST_EVAL_JOB_ENABLED` | MLOps | Backfill realized MAPE → closes the loop | None — read-only evaluation |
| 5 | `FORECAST_ACCURACY_FEEDBACK_ENABLED` | W2 | Auto-flag accuracy degradation | Low — audit rows only |
| 6 | `FORECAST_THRESHOLD_CALIBRATION_ENABLED` | W1 | Gate decision-logger for future calibration | None — logging gate |
| 7 | `FORECAST_THRESHOLD_AUTOTUNE_ENABLED` | W2 | Nightly threshold auto-tune (staging only) | Low — staged, not auto-applied |
| 8 | `FORECAST_DRIFT_AUTO_ADJUST_ENABLED` | MLOps | Auto-adjust ensemble weights on drift | Medium — changes weights, blend=0.2 cap |
| 9 | `FORECAST_EVENT_CALIBRATION_ENABLED` | W2 | Event-study calibration for intel overlay | Low — replaces heuristic |
| 10 | `FORECAST_HITL_ENABLED` | MLOps | Submit corrections via 修正预测 modal | None — modal already built |
| 11 | `FORECAST_BIAS_CORRECTION_ENABLED` | MLOps | Apply bias correction after 3+ scored overrides | Medium — moves price; trust gate |
| 12 | `FORECAST_EXTERNAL_EXOG_ENABLED` | W3 | XGBoost exog from external feeds | Blocked — needs CSV data first |
| 13 | `FORECAST_EXTERNAL_SIGNAL_ENABLED` | W3 | AI Brief renders external signals | Blocked — needs CSV data first |

**Per-flag validation protocol:**
1. Run baseline backtest (all flags OFF) → save MAPE per product per horizon
2. Turn ON one flag → `docker compose up -d --force-recreate backend`
3. Re-run backtest → compare MAPE delta per product
4. Keep ON if: aggregate MAPE improves OR per-product MAPE improves for ≥60% of products AND no product degrades >20%
5. Revert if: aggregate MAPE worsens OR any product degrades >20%
6. Log decision in `docs/superpowers/plans/flag-validation-log.md`

### Stream B — Close the Decision Loop (3-5 days, frontend-heavy)

**Goal:** Surface built-but-invisible infrastructure to make forecasts actionable.

**B1. ROI Track Record in Decision Board** (highest value)
- Backend: `decision_board_service.py` — query `decision_roi.aggregate_roi()` per product, add `decision_roi` field to `DecisionBoardProduct` schema
- Frontend: `DecisionBoard.jsx` — add "决策命中率" column showing % of past buy/sell signals that were directionally correct
- Files: `backend/app/schemas/market_dashboard.py`, `backend/app/services/decision_board_service.py`, `frontend/src/pages/MarketDashboard/components/DecisionBoard.jsx`

**B2. Explainability Panel** ("why this forecast")
- `explain.py` already has `Explanation.drivers` (top-5 XGBoost feature importances) — built but not surfaced
- Backend: expose `drivers` field in forecast analysis endpoint
- Frontend: add collapsible "预测依据" panel in DecisionBoard ExpandedCard showing top drivers with weights
- Files: `backend/app/services/market_dashboard_service.py` (get_forecast_analysis), `DecisionBoard.jsx`

**B3. Scenario Planning UI** ("what if naphtha +10%?")
- NO stress-test module exists yet (`scenarios.py` only does base/bull/bear from residuals)
- Backend: create `stress_test.py` — apply user-specified shock to feedstock forecast, re-run ensemble, return counterfactual
- Frontend: add "情景模拟" button in ExpandedCard → modal with feedstock selector + shock slider + counterfactual chart
- Files: `backend/app/services/forecasting/stress_test.py` (new), `backend/app/routers/forecast_ops.py` (new endpoint), `DecisionBoard.jsx`

**B4. Drift/Signal Alert System**
- `forecast_ops.py /status` already returns drift chips — built but no proactive notification
- Backend: create `alert_service.py` — polling job that checks `/status` + decision signals, writes `ForecastAlert` rows
- Frontend: add alert bell icon in MarketDashboard header → dropdown of recent alerts
- Files: `backend/app/services/forecasting/alert_service.py` (new), `backend/app/models/forecasting.py` (new model), `frontend/src/pages/MarketDashboard/`

**B5. HITL Trust Gate** (already built, just needs enablement)
- `ForecastFeedbackModal.jsx` is fully wired (submit + history + hit/miss)
- `FORECAST_HITL_ENABLED=false` in `.env` → turn ON (Stream A flag #10)
- Trust gate activates after 3+ scored overrides per author → bias correction eligible

### Stream C — Break the Data Ceiling (long-term, blocked on external data)

**Goal:** Unblock Tier 3 fundamental data feeds.

**C1. Procurement Questionnaire** (action item — costs nothing, starts the long pole)
- Draft email questionnaire for procurement/market-intel team
- Asks: existing subscriptions (隆众/卓创/ICIS/Argus/Wind), operating rate data, port inventory, import prices, HS code mappings
- Deliverable: `docs/superpowers/plans/tier3-procurement-questionnaire.md`

**C2. HS Code Mapping for Customs Import Data** (free data, just needs mapping)
- China customs import data is publicly available
- Need to map petrochemical HS codes for: naphtha, styrene, isoprene, piperylene, DCPD, cracked C5/C9
- Deliverable: `docs/superpowers/plans/hs-code-mapping.md` with HS codes + customs data source URLs

**C3. Internal ERP/MES Audit** (free proxy data)
- Audit internal ERP/MES for: production volumes, inventory levels, operating rates
- These are free proxies for Tier 3 data that's blocked on procurement
- Deliverable: `docs/superpowers/plans/internal-data-audit.md` with table inventory + loader specs

---

## Execution Order (This Session)

1. ✅ Write this plan doc
2. **Stream A:** Run Wave 1 tests to verify wiring is solid → run baseline backtest
3. **Stream C1:** Draft procurement questionnaire (quick, unblocks long pole)
4. **Stream B1:** Add ROI track record to Decision Board (highest-value surface work)
5. **Stream A:** Turn on flag #1 (`FORECAST_ERP_VOLUME_EXOG_ENABLED`) + re-run backtest
6. **Stream B2:** Add explainability panel (if time permits)
7. **Stream C2:** Map HS codes (if time permits)
8. Append memory note

## Key Constraints

- **SSH RAM limited** — run tests individually, no backgrounding
- **Docker**: `.env` flag changes need `docker compose up -d --force-recreate backend`
- **Zero regression**: all flags default OFF, A/B validated before keeping ON
- **Chinese market colors**: 涨=red, 跌=green
- **Backend venv**: `cd backend && venv/bin/python -m pytest`
