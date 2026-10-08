# Flag Validation Log — A/B Backtest Results

> Tracks the A/B validation results for each forecast feature flag.
> Baseline = all flags OFF (except demand_signal=display-only + decision_logging=logging-only).

## Baseline (2026-08-06, flags OFF)

**Backtest:** `walk_forward_backtest.py` — 8 products, h=7/14/30, walk-forward.

| Product | Len | MAPE7d | MAPE14d | MAPE30d | Dir7d | Dir30d |
|---------|-----|--------|---------|---------|------|--------|
| Product_C | 110 | 7.0% | 11.2% | 19.4% | 0% | 50% |
| Cracked C5 | 203 | 2.9% | 5.5% | 8.7% | 10% | 40% |
| Cracked C9 | 73 | 19.8% | 33.8% | 44.5% | 0% | 0% |
| Product_D | 88 | 10.9% | 20.0% | 25.1% | 33% | 67% |
| Product F | 85 | 29.6% | 42.9% | 52.4% | 50% | 50% |
| PRODUCT_B | 74 | 1.4% | 3.8% | 11.3% | 0% | 0% |
| SIS | 85 | 10.0% | 12.3% | 15.4% | 100% | 100% |
| Styrene | 69 | 9.5% | 8.7% | 11.6% | 0% | 0% |

**Aggregate:**
- 7-day: MAPE=11.4%, DirAcc=24.2%
- 14-day: MAPE=17.3%, DirAcc=30.4%
- 30-day: MAPE=23.5%, DirAcc=38.3%

**Key observations:**
- XGBoost models returning `inf%` MAPE — failing (insufficient exog features or data issues)
- Ensemble worse than naive for Styrene (29.0% vs 13.6%) — ensemble includes inf-MAPE models
- Cracked C5 best performer (2.9% at 7d)
- Product F worst performer (29.6% at 7d)
- Directional accuracy generally poor (24-38%) — below 50% random baseline for 7d/14d

## Flag: FORECAST_DEMAND_SIGNAL_ENABLED=true (2026-08-06)

**Status:** ON — display-only (no model impact), safe to keep.
**Verification:** 2/4 upstream products now show demand signal in AI Brief 供需研判 section (原油, 裂解材料M have ERP volume data).

## Flag: FORECAST_DECISION_LOGGING_ENABLED=true (2026-08-06)

**Status:** ON — logging-only (no model impact), safe to keep.
**Verification:** Next forecast run will create ForecastDecisionLog rows → B1 ROI track record will populate.

## Pending A/B Tests

| Flag | Status | Expected effect | A/B needed |
|------|--------|-----------------|------------|
| FORECAST_ERP_VOLUME_EXOG_ENABLED | OFF | ERP volume → XGBoost exog. Could fix inf-MAPE XGBoost issue | YES — highest priority |
| FORECAST_EVAL_JOB_ENABLED | OFF | Backfill realized MAPE | No (read-only) |
| FORECAST_DRIFT_AUTO_ADJUST_ENABLED | OFF | Auto-adjust weights on drift | YES |
| FORECAST_HITL_ENABLED | OFF | Submit corrections | No (modal built) |
| FORECAST_BIAS_CORRECTION_ENABLED | OFF | Bias correction after 3+ overrides | YES (moves price) |
