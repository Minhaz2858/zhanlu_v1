# Forecasting Accuracy Improvement Design

**Date**: 2026-08-07  
**Status**: Design approved, implementation in-progress  
**Baseline**: 7d MAPE ~11.4%, DirAcc 24-38%, 10 products, 8-model ensemble

## Root Causes

1. **Hardcoded XGBoost params** — `n_estimators=100, max_depth=3, lr=0.1` — suboptimal for most products. Existing `xgboost_tuner.py` (Optuna Bayesian) is never called.
2. **No feature selection** — all 10+ exogenous features fed to XGBoost indiscriminately, injecting noise.
3. **Recursive error compounding** — 7-day forecast recursively feeds day-1 prediction into day-2, amplifying errors.
4. **No regime awareness in ensemble blending** — equal model weights regardless of market state.
5. **Directional signal unused** — `directional_classifier.py` exists and backtests but predictions don't influence forecasts.
6. **Basic outlier handling** — only STL spike detection; no ensemble anomaly detection.
7. **Oilchem upstream data loaded but not enabled** — code path exists, flag is off.

## Three Improvements

### #1: Bayesian XGBoost Tuning + Feature Selection
- Wire `tune_xgboost_params()` into `XGBoostReg.fit()` (Optuna TPE + purged CV, cached per-product in `/tmp/xgb_tuning/`)
- Create `feature_selector.py` — permutation importance, drops bottom 30% features, caches in `/tmp/xgb_features/`
- **Expected**: 7d MAPE 11.4% → 8-9%
- **Flags**: `FORECAST_XGB_TUNING_ENABLED`, `FORECAST_FEATURE_SELECTION_ENABLED`

### #2: Market Regime Detection + Directional Classifier
- Create `regime_detector.py` — classifies bull/bear/volatile/sideways from 30d rolling stats
- Add regime param to `blend()` with weight multipliers
- Integrate `DirectionalClassifier.predict_proba()` into ensemble directional signal
- **Expected**: DirAcc 24-38% → 45-55%
- **Flags**: `FORECAST_REGIME_DETECTION_ENABLED`, `FORECAST_DIRECTIONAL_CLASSIFIER_ENABLED`

### #3: Enhanced Outlier Detection + Oilchem Integration
- Create `anomaly_detector.py` — Isolation Forest + STL Z-score + rolling IQR ensemble (majority vote)
- Replace anomalous values with window median
- Enable `FORECAST_OILCHEM_EXOG_ENABLED`
- **Expected**: +0.5-1% additional MAPE reduction
- **Flags**: `FORECAST_ANOMALY_DETECTION_ENABLED`, `FORECAST_OILCHEM_EXOG_ENABLED`

## Architecture Integration Points

```
data → preprocess(+anomaly) → features(+oilchem+feature_select) → fit(+tuning) → backtest
    → detect_regime → blend(+regime_mult) → directional_predict → explain(+regime,+signal) → cache
```

All improvements flag-gated (default false) for safe A/B rollout.

## Rollback Plan
- Set any flag to `false` → behavior reverts to pre-improvement baseline
- Cached tuning/feature files in `/tmp/` can be deleted without impact
