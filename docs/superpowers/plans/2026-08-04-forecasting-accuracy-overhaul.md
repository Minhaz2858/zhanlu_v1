# Forecasting Accuracy Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the forecast engine from a point-estimate ensemble (11.4% / 23.5% MAPE at 7d/30d, 24-38% directional accuracy — below random) into a calibrated probabilistic + decision-support system: better point accuracy, honest prediction intervals, price-change probabilities, and buy/hold/sell signals.

**Architecture:** Four independently-shippable phases layered on the existing 8-model ensemble pipeline. Phase C fixes the dishonest backtest + per-horizon weighting (foundation). Phase D adds split-conformal calibrated intervals + price-change probability. Phase E adds a directional classifier + decision engine. Phase F extends data history (mining EDIA MySQL tables that go back to 2018) + enables the causal domain-signal chain. Each phase ships with tests and a backtest A/B gate.

**Tech Stack:** Python 3.11, pandas, numpy, scikit-learn (already a dependency for XGBoost features), statsmodels (ARIMA/ETS), existing `ForecastModel` base class. No new heavy deps (no torch — Chronos deferred).

## Global Constraints

- **Project root**: `/home/ysk2025/zhanlu_7_30` (NOT `/home/ysk2025/zhanlu` or `zhanlu-deploy`).
- **Backend venv**: `backend/venv/bin/python` (system has no pytest; pytest not in requirements.txt). Run tests via `cd backend && venv/bin/python -m pytest <path> -v`.
- **No `--reload`**: after service changes, `docker compose restart backend`. Backend exposes port **5002**.
- **User works on `main`** directly with uncommitted changes — commit per task, do not stash.
- **SSH RAM limited**: run test files individually (4+ files in one invocation timeout).
- **EDIA MySQL**: `mysql+pymysql://root:...@10.10.10.49:3306/aipdp_data_warehouse_prod`, env var `EDIA_MYSQL_URL`. Password in `.env`.
- **ForecastRun model gotchas**: no `horizons` field; `confidence` is `String(50)` not float; `below_naive_baseline` is bool. Adjustment target in engine = `guard_result.published_forecast`.
- **Engine step order** (engine.py `compute_target()`): …→ ensemble → guard → scenarios → **Step 8.5** intel overlay (~line 337, gated `FORECAST_INTELLIGENCE_OVERLAY_ENABLED`) → **Step 8.55** domain signals (gated `FORECAST_DOMAIN_SIGNALS_ENABLED`, default **False**) → **Step 8.6** policy service (~line 380) → explain (~line 449, explanation dict has `intelligence`+`policy`+`domain_signals`+`trust_tier` keys).
- **TDD discipline**: write failing test → run (confirm fail) → implement → run (confirm pass) → commit. One logical change per commit.
- **No Alembic migration needed** for Phases C-E (no schema changes to ForecastRun). Phase F may add a `data_sources` JSON column extension to `ForecastTarget.datasource` (already JSON — no migration).

---

## File Structure

### New files (Phase C-D-E-F)
- `backend/app/services/forecasting/conformal.py` — split-conformal prediction intervals (Phase D)
- `backend/app/services/forecasting/price_change_probability.py` — P(price[T+h] > price[T]) (Phase D)
- `backend/app/services/forecasting/directional_classifier.py` — trend up/down classifier (Phase E)
- `backend/app/services/forecasting/decision_engine.py` — buy/hold/sell recommendation (Phase E)
- `backend/app/services/forecasting/erp_feature_source.py` — pull sale_erp_v_* history as exogenous features (Phase F)
- `backend/tests/test_conformal.py` (Phase D)
- `backend/tests/test_price_change_probability.py` (Phase D)
- `backend/tests/test_directional_classifier.py` (Phase E)
- `backend/tests/test_decision_engine.py` (Phase E)
- `backend/tests/test_walk_forward_backtest_unit.py` (Phase C)
- `backend/tests/test_per_horizon_ensemble.py` (Phase C)
- `backend/tests/test_erp_feature_source.py` (Phase F)

### Modified files
- `backend/app/services/forecasting/backtest.py` — rewrite to true walk-forward + collect per-step residuals + per-horizon MAPE (Phase C)
- `backend/app/services/forecasting/ensemble.py` — per-horizon weight matrix, drop floor to 0.0, MAPE-threshold gating (Phase C)
- `backend/app/services/forecasting/models/__init__.py` — XGBoost min-rows gating in `build_model_pool()` (Phase C)
- `backend/app/services/forecasting/engine.py` — wire per-horizon ensemble, conformal, classifier, decision, multi-source (all phases)
- `backend/app/services/forecasting/scenarios.py` — use conformal bands instead of 25/75 percentile (Phase D)
- `backend/app/services/forecasting/edia_source.py` — add `read_history_multi()` for source concatenation (Phase F)
- `backend/app/services/forecasting/seed_ecisco_targets.py` — add lz_v extended sources for isoprene/styrene (Phase F)
- `backend/app/services/forecasting/features/feature_builder.py` — add ERP lag features (Phase F)
- `backend/app/schemas/market_dashboard.py` — add `prediction_interval`, `price_change_probability`, `direction`, `decision` fields (Phases D-E)
- `frontend/src/pages/MarketDashboard/components/ForecastTrendChart.jsx` — calibrated probability bands + decision badge (Phases D-E)

### Reference (read-only, EDIA source for porting)
- `backend/tests/walk_forward_backtest.py` — proven walk-forward script (port logic into `backtest.py`)
- `EDIA_5.1.2/backend/src/service/forecasting_engine.py:113-179` — elasticities + seasonal rules (already ported to `domain_signals.py`)

---

## Phase C — Honest Foundation (Point Accuracy)

**Why first**: the current backtest is in-sample expanding-window (optimistic MAPE), the ensemble dilutes ARIMA (single weight across all horizons), XGBoost returns inf% on short series, and `backtest.py:114` never populates `residuals` (dead code → scenarios fall back to ±sigma). Fixing this is the foundation for calibrated intervals in Phase D.

### Task C1: Rewrite backtest to true walk-forward + collect residuals

**Files:**
- Modify: `backend/app/services/forecasting/backtest.py`
- Test: `backend/tests/test_walk_forward_backtest_unit.py`

**Interfaces:**
- Produces: `BacktestResult` with new fields `per_horizon_mape: dict[int, dict[str, float]]` (horizon→model→mape), `residuals_by_horizon: dict[int, list[float]]` (horizon→residuals), and `directional_accuracy: dict[str, float]` (model→% correct sign). Keeps existing `per_model_mape`, `ensemble_mape`, `naive_mape`, `n_folds`, `metric` for backward compat.
- Consumes: existing `ForecastModel.fit(y, seasonal_period)` / `.forecast(h)`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_walk_forward_backtest_unit.py
import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.backtest import evaluate, BacktestResult
from app.services.forecasting.models.naive import NaiveLast, SeasonalNaive
from app.services.forecasting.models.arima import ARIMA


def _trend_series(n=120):
    """Upward-trending series with noise — directional signal present."""
    rng = np.random.default_rng(42)
    t = np.arange(n, dtype=float)
    return pd.Series(100 + 0.5 * t + rng.normal(0, 2, n), name="y")


def test_backtest_returns_per_horizon_mape():
    y = _trend_series(120)
    models = {"naive_last": NaiveLast(), "arima": ARIMA()}
    result = evaluate(y, models, seasonal_period=7, horizons=[7, 30])
    assert isinstance(result, BacktestResult)
    # Per-horizon MAPE populated for each requested horizon
    assert set(result.per_horizon_mape.keys()) == {7, 30}
    for h, model_map in result.per_horizon_mape.items():
        assert "arima" in model_map
        assert model_map["arima"] < float("inf")


def test_backtest_collects_residuals_by_horizon():
    """The old backtest left residuals empty (bug). New one must populate."""
    y = _trend_series(120)
    models = {"naive_last": NaiveLast()}
    result = evaluate(y, models, seasonal_period=7, horizons=[7])
    assert 7 in result.residuals_by_horizon
    assert len(result.residuals_by_horizon[7]) >= 5  # enough for percentiles


def test_backtest_walk_forward_no_train_test_overlap():
    """Origin slides forward; each fold's test window must not overlap its train."""
    y = _trend_series(60)
    models = {"naive_last": NaiveLast()}
    result = evaluate(y, models, seasonal_period=7, horizons=[7], step=7)
    assert result.n_folds >= 2
    # If overlap existed, residuals would be suspiciously small; sanity-check mape > 0
    assert result.per_model_mape["naive_last"] > 0


def test_backtest_directional_accuracy_populated():
    y = _trend_series(120)
    models = {"naive_last": NaiveLast(), "arima": ARIMA()}
    result = evaluate(y, models, seasonal_period=7, horizons=[7])
    assert "arima" in result.directional_accuracy
    # On a clean uptrend, ARIMA should beat 50% directional
    assert result.directional_accuracy["arima"] >= 0.5


def test_backtest_short_series_returns_empty():
    y = pd.Series([100, 101, 102], name="y")
    result = evaluate(y, {"naive_last": NaiveLast()}, horizons=[7])
    assert result.n_folds == 0
    assert result.per_horizon_mape == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && venv/bin/python -m pytest tests/test_walk_forward_backtest_unit.py -v`
Expected: FAIL — `evaluate()` does not accept `horizons`/`step` params; `per_horizon_mape`/`residuals_by_horizon`/`directional_accuracy` attributes don't exist.

- [ ] **Step 3: Implement walk-forward backtest**

Rewrite `backtest.py`. Key changes:
1. `evaluate()` gains `horizons: list[int] = [7, 14, 30]` and `step: int = 14` params (sliding origin, not expanding-only).
2. For each origin `T` (from `min_train` to `n - max(horizons)`, stepping by `step`): train on `y[:T]`, forecast `h=max(horizons)`, compare actuals `y[T:T+h]` at each requested horizon.
3. Collect **per-step residuals** `actual[h] - pred[h]` into `residuals_by_horizon[h]`.
4. Compute `per_horizon_mape[h][model]` = mean APE across folds at that horizon.
5. Compute `directional_accuracy[model]` = fraction of folds where `sign(pred[h]-y[T]) == sign(y[T+h]-y[T])` at the primary horizon (7d).
6. **Fix the dead-code bug**: actually append to the residuals accumulator (old code declared `all_residuals = []` but never appended).
7. Keep `per_model_mape` (averaged across horizons) and `ensemble_mape` for backward compat.

```python
# backtest.py — new BacktestResult + evaluate signature
@dataclass
class BacktestResult:
    per_model_mape: dict[str, float]            # backward-compat: avg across horizons
    ensemble_mape: float
    naive_mape: float
    n_folds: int
    residuals: list[float]                       # backward-compat: flattened
    metric: str = "MAPE"
    # NEW:
    per_horizon_mape: dict[int, dict[str, float]] = field(default_factory=dict)
    residuals_by_horizon: dict[int, list[float]] = field(default_factory=dict)
    directional_accuracy: dict[str, float] = field(default_factory=dict)


def evaluate(
    y: pd.Series,
    models: dict[str, ForecastModel],
    seasonal_period: int = 7,
    min_train: int = 30,
    max_folds: int = 10,
    horizons: list[int] | None = None,   # NEW
    step: int = 14,                       # NEW: sliding-origin step
) -> BacktestResult:
    ...
```

See `backend/tests/walk_forward_backtest.py` (the proven E2E script) for the sliding-origin loop structure — port its origin-iteration logic, but produce the richer `BacktestResult`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && venv/bin/python -m pytest tests/test_walk_forward_backtest_unit.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Run existing backtest-dependent tests to confirm no regression**

Run: `cd backend && venv/bin/python -m pytest tests/test_forecast_tool.py -v`
Expected: 36 PASS (existing). If any fail due to the new `evaluate` signature, update call sites in `engine.py` to pass `horizons` — the new params have defaults so calls without them should still work, but verify.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/forecasting/backtest.py backend/tests/test_walk_forward_backtest_unit.py
git commit -m "feat(forecast): walk-forward backtest with per-horizon MAPE + residual collection"
```

---

### Task C2: Per-horizon ensemble weight matrix

**Files:**
- Modify: `backend/app/services/forecasting/ensemble.py`
- Test: `backend/tests/test_per_horizon_ensemble.py`

**Interfaces:**
- Produces: `blend()` accepts optional `per_model_error_by_horizon: dict[int, dict[str, float]] | None`. When provided, computes a **per-step weight vector** (weight at forecast step `t` uses the error at the nearest horizon). `EnsembleResult.weights` becomes `dict[str, list[float]]` (weight per step) when per-horizon; stays `dict[str, float]` (flat) when not provided — backward compatible.
- New constant `_MAX_ERROR_RATIO = 2.0` — models with MAPE > 2× the best model's MAPE get zero weight (drop floor pollution).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_per_horizon_ensemble.py
import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.ensemble import blend, EnsembleResult


def _fc(name, vals):
    return pd.Series(vals, name=name)


def test_per_horizon_weights_vary_by_step():
    """ARIMA best at h=1..7, ETS best at h=20..30 → weights shift along horizon."""
    h = 30
    forecasts = {
        "arima": _fc("arima", np.linspace(100, 115, h)),
        "ets": _fc("ets", np.linspace(100, 118, h)),
    }
    # ARIMA low error at 7d, high at 30d; ETS opposite
    per_h = {
        7: {"arima": 0.03, "ets": 0.12},
        30: {"arima": 0.20, "ets": 0.05},
    }
    result = blend(forecasts, per_model_error={"arima": 0.10, "ets": 0.10},
                   per_model_error_by_horizon=per_h)
    assert isinstance(result, EnsembleResult)
    # weights is now per-step list
    assert isinstance(result.weights["arima"], list)
    assert len(result.weights["arima"]) == h
    # At step 7 ARIMA should outweigh ETS; at step 30 vice-versa
    assert result.weights["arima"][6] > result.weights["ets"][6]
    assert result.weights["arima"][29] < result.weights["ets"][29]


def test_floor_removed_bad_model_gets_zero():
    """A model 3x worse than best gets zero weight (no floor rescue)."""
    forecasts = {"good": _fc("good", [100]*7), "bad": _fc("bad", [100]*7)}
    result = blend(forecasts, per_model_error={"good": 0.05, "bad": 0.20})
    assert result.weights["bad"] == 0.0 or result.weights["bad"] < 0.01
    assert result.weights["good"] > 0.99


def test_flat_weights_backward_compat():
    """No per-horizon dict → flat weights (old behavior)."""
    forecasts = {"a": _fc("a", [100]*7), "b": _fc("b", [101]*7)}
    result = blend(forecasts, per_model_error={"a": 0.05, "b": 0.06})
    assert isinstance(result.weights["a"], float)
    assert abs(sum(result.weights.values()) - 1.0) < 1e-9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && venv/bin/python -m pytest tests/test_per_horizon_ensemble.py -v`
Expected: FAIL — `blend()` has no `per_model_error_by_horizon` param; `weights` is always flat dict.

- [ ] **Step 3: Implement per-horizon blending**

In `ensemble.py`:
1. Change `_FLOOR_WEIGHT = 0.05` → `_FLOOR_WEIGHT = 0.0`.
2. Add `_MAX_ERROR_RATIO = 2.0`.
3. In `blend()`: after computing softmax weights, zero out any model whose error > `_MAX_ERROR_RATIO * min(errors)`, renormalize.
4. Add `per_model_error_by_horizon` param. When provided: for each forecast step `t` (0-indexed), pick the horizon `h*` = nearest requested horizon ≥ `t+1`; look up `per_model_error_by_horizon[h*]`; compute softmax weights for that step; build `weights[name]` as a list of per-step weights. Blend = sum over steps of `w_t[name] * fc[name][t]`.
5. When `per_model_error_by_horizon` is None: keep flat-weight path (backward compat), but still apply the zero-floor + ratio gating.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && venv/bin/python -m pytest tests/test_per_horizon_ensemble.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/forecasting/ensemble.py backend/tests/test_per_horizon_ensemble.py
git commit -m "feat(forecast): per-horizon ensemble weights + drop floor, ratio-gate bad models"
```

---

### Task C3: XGBoost min-rows gating + wire per-horizon into engine

**Files:**
- Modify: `backend/app/services/forecasting/models/__init__.py`
- Modify: `backend/app/services/forecasting/engine.py`
- Test: extend `backend/tests/test_per_horizon_ensemble.py` or new `test_model_pool_gating.py`

**Interfaces:**
- `build_model_pool(y: pd.Series, ...)` — gains awareness of series length; skips `XGBoostReg`/`XGBoostExog` when `len(y) < _MIN_ML_ROWS` (default 90).
- `engine.compute_target()` — passes `backtest_result.per_horizon_mape` into `blend(per_model_error_by_horizon=...)`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_model_pool_gating.py
import pandas as pd
import pytest
from app.services.forecasting.models import build_model_pool


def test_xgboost_skipped_on_short_series():
    y = pd.Series(range(70), dtype=float)  # < 90 rows
    pool = build_model_pool(y)
    assert "xgboost_reg" not in pool
    assert "xgboost_exog" not in pool
    assert "arima" in pool  # statistical models stay


def test_xgboost_present_on_long_series():
    y = pd.Series(range(120), dtype=float)
    pool = build_model_pool(y)
    assert "xgboost_reg" in pool
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && venv/bin/python -m pytest tests/test_model_pool_gating.py -v`
Expected: FAIL — `build_model_pool` doesn't accept `y` or filter by length.

- [ ] **Step 3: Implement gating + wire engine**

1. In `models/__init__.py`: add `_MIN_ML_ROWS = 90`. Change `build_model_pool()` to accept `y: pd.Series | None = None` (default None for backward compat); when `y` is provided and `len(y) < _MIN_ML_ROWS`, omit the two XGBoost models from the returned dict.
2. In `engine.py compute_target()`: find where `build_model_pool()` is called (search for it) and pass `y`. Find where `blend(...)` is called and pass `per_model_error_by_horizon=backtest_result.per_horizon_mape` (guard: only if non-empty).

- [ ] **Step 4: Run tests to verify pass**

Run: `cd backend && venv/bin/python -m pytest tests/test_model_pool_gating.py tests/test_forecast_tool.py -v`
Expected: gating tests PASS; 36 forecast_tool tests still PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/forecasting/models/__init__.py backend/app/services/forecasting/engine.py backend/tests/test_model_pool_gating.py
git commit -m "feat(forecast): gate XGBoost on min 90 rows + wire per-horizon ensemble into engine"
```

---

### Task C4: Backtest A/B comparison — verify accuracy improved

**Files:**
- Modify: `backend/tests/walk_forward_backtest.py` (the E2E script) — add a `--compare` mode that runs both old-ensemble and new-per-horizon-ensemble and prints a delta table.

- [ ] **Step 1: Add comparison harness**

In `walk_forward_backtest.py`, add a function `compare_strategies(products)` that for each product runs the walk-forward backtest twice: (a) flat-weight ensemble (old), (b) per-horizon ensemble (new), and prints `7d/14d/30d MAPE` for both + delta. Gate behind `if __name__ == "__main__": --compare`.

- [ ] **Step 2: Run the comparison**

Run: `cd backend && venv/bin/python tests/walk_forward_backtest.py --compare 2>&1 | tail -40`
Expected: per-horizon ensemble MAPE ≤ flat-weight MAPE on most products (especially where ARIMA dominates at 7d). Capture the table for the plan's verification section.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/walk_forward_backtest.py
git commit -m "test(forecast): A/B comparison harness for flat vs per-horizon ensemble"
```

---

## Phase D — Probabilistic Forecasting (Decision Support)

**Why**: scenarios currently use 25th/75th residual percentiles (or ±sigma fallback because residuals were empty — now fixed in C1). Users get bull/bear but no calibrated probability statements. Split-conformal gives coverage guarantees (true 90% of the time) and enables "78% chance price rises ≥3%".

### Task D1: Split-conformal prediction intervals

**Files:**
- Create: `backend/app/services/forecasting/conformal.py`
- Test: `backend/tests/test_conformal.py`

**Interfaces:**
- `calibrate(residuals_by_horizon: dict[int, list[float]], alpha: float = 0.1) -> ConformalCalibration` — computes per-horizon quantile of |residual| at the `1-alpha` level.
- `ConformalCalibration.interval(point_forecast: pd.Series, horizon: int) -> tuple[pd.Series, pd.Series]` — returns (lower, upper) with empirical coverage ≈ `1-alpha`.
- `ConformalCalibration.coverage_check(...)` — helper asserting calibration is finite.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_conformal.py
import numpy as np
import pandas as pd
import pytest
from app.services.forecasting.conformal import calibrate, ConformalCalibration


def test_calibrate_returns_intervals_per_horizon():
    rng = np.random.default_rng(0)
    res = {7: list(rng.normal(0, 3, 200)), 30: list(rng.normal(0, 8, 200))}
    cal = calibrate(res, alpha=0.1)
    assert isinstance(cal, ConformalCalibration)
    point = pd.Series(np.linspace(100, 110, 30))
    lo, hi = cal.interval(point, horizon=7)
    assert len(lo) == 7 and len(hi) == 7
    assert (hi > lo).all()
    assert (lo < point.iloc[:7]).all()
    assert (hi > point.iloc[:7]).all()


def test_wider_interval_for_longer_horizon():
    """30d residuals are larger → wider interval than 7d."""
    rng = np.random.default_rng(1)
    res = {7: list(rng.normal(0, 2, 200)), 30: list(rng.normal(0, 10, 200))}
    cal = calibrate(res, alpha=0.1)
    point = pd.Series([100.0] * 30)
    lo7, hi7 = cal.interval(point, horizon=7)
    lo30, hi30 = cal.interval(point, horizon=30)
    assert (hi7.iloc[0] - lo7.iloc[0]) < (hi30.iloc[0] - lo30.iloc[0])


def test_empty_residuals_fallback_sigma():
    cal = calibrate({}, alpha=0.1)
    point = pd.Series([100.0] * 7)
    lo, hi = cal.interval(point, horizon=7)
    # Falls back to ±10% sigma
    assert abs((hi.iloc[0] - lo.iloc[0]) - 20.0) < 0.01
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && venv/bin/python -m pytest tests/test_conformal.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement conformal calibration**

```python
# backend/app/services/forecasting/conformal.py
"""Split-conformal prediction intervals.

Calibrated from walk-forward residuals (Phase C BacktestResult.residuals_by_horizon).
For each horizon h, the half-width = quantile(|residual_h|, 1-alpha). This gives
empirical coverage ~1-alpha under exchangeability of residuals.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np
import pandas as pd

_DEFAULT_ALPHA = 0.1  # 90% intervals
_FALLBACK_SIGMA_FRAC = 0.10  # ±10% of |point| when no residuals


@dataclass
class ConformalCalibration:
    half_widths: dict[int, float]  # horizon → half-width
    alpha: float = _DEFAULT_ALPHA
    fallback_sigma_frac: float = _FALLBACK_SIGMA_FRAC

    def interval(self, point_forecast: pd.Series, horizon: int) -> tuple[pd.Series, pd.Series]:
        h = min(horizon, len(point_forecast))
        hw = self.half_widths.get(horizon)
        if hw is None or not np.isfinite(hw):
            hw = self.fallback_sigma_frac * (float(np.abs(point_forecast.iloc[:h]).mean()) + 1e-10)
        base = point_forecast.iloc[:h]
        return (base - hw), (base + hw)


def calibrate(residuals_by_horizon: dict[int, list[float]], alpha: float = _DEFAULT_ALPHA) -> ConformalCalibration:
    half_widths: dict[int, float] = {}
    for h, res in residuals_by_horizon.items():
        if not res or len(res) < 2:
            continue
        abs_res = np.abs(np.array(res, dtype=float))
        abs_res = abs_res[np.isfinite(abs_res)]
        if len(abs_res) == 0:
            continue
        half_widths[h] = float(np.quantile(abs_res, 1 - alpha))
    return ConformalCalibration(half_widths=half_widths, alpha=alpha)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && venv/bin/python -m pytest tests/test_conformal.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/forecasting/conformal.py backend/tests/test_conformal.py
git commit -m "feat(forecast): split-conformal calibrated prediction intervals"
```

---

### Task D2: Price-change probability

**Files:**
- Create: `backend/app/services/forecasting/price_change_probability.py`
- Test: `backend/tests/test_price_change_probability.py`

**Interfaces:**
- `compute(point_forecast: pd.Series, last_actual: float, calibration: ConformalCalibration, horizon: int) -> PriceChangeProbability` with fields `p_rise: float` (P(price[T+h] > last_actual)), `p_rise_gt: dict[float, float]` (P(rise > X%)), `expected_change_pct: float`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_price_change_probability.py
import numpy as np
import pandas as pd
import pytest
from app.services.forecasting.conformal import ConformalCalibration
from app.services.forecasting.price_change_probability import compute, PriceChangeProbability


def test_p_rise_above_half_when_forecast_up():
    cal = ConformalCalibration(half_widths={7: 3.0})
    point = pd.Series([105.0] * 7)  # forecast rises from 100
    res = compute(point, last_actual=100.0, calibration=cal, horizon=7)
    assert res.p_rise > 0.5
    assert 0.0 <= res.p_rise <= 1.0


def test_p_rise_below_half_when_forecast_down():
    cal = ConformalCalibration(half_widths={7: 3.0})
    point = pd.Series([96.0] * 7)
    res = compute(point, last_actual=100.0, calibration=cal, horizon=7)
    assert res.p_rise < 0.5


def test_threshold_probabilities():
    """P(rise > 5%) should be < P(rise > 0%)."""
    cal = ConformalCalibration(half_widths={30: 8.0})
    point = pd.Series([104.0] * 30)
    res = compute(point, last_actual=100.0, calibration=cal, horizon=30,
                  thresholds=[0.0, 0.03, 0.05])
    assert res.p_rise_gt[0.0] > res.p_rise_gt[0.05]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && venv/bin/python -m pytest tests/test_price_change_probability.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement**

```python
# backend/app/services/forecasting/price_change_probability.py
"""P(price[T+h] > price[T]) from conformal interval + point forecast.

Assumes forecast errors are ~Gaussian centered on the point forecast
(conformal half-width ≈ z_{1-alpha/2} * sigma). Derives sigma from the
half-width, then computes the normal CDF of the price-change.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from scipy.stats import norm
import numpy as np
import pandas as pd
from app.services.forecasting.conformal import ConformalCalibration

_DEFAULT_THRESHOLDS = [0.0, 0.02, 0.05]


@dataclass
class PriceChangeProbability:
    p_rise: float
    p_rise_gt: dict[float, float] = field(default_factory=dict)
    expected_change_pct: float = 0.0


def compute(point_forecast, last_actual, calibration, horizon, thresholds=None):
    if thresholds is None:
        thresholds = _DEFAULT_THRESHOLDS
    h = min(horizon, len(point_forecast))
    point_h = float(point_forecast.iloc[h - 1])
    hw = calibration.half_widths.get(horizon, calibration.fallback_sigma_frac * abs(point_h) + 1e-10)
    # half-width at alpha=0.1 ≈ 1.645 * sigma → sigma = hw / 1.645
    sigma = hw / 1.645 if hw > 0 else abs(point_h) * 0.05 + 1e-6
    delta = point_h - last_actual
    p_rise = float(norm.cdf(delta / sigma))
    p_rise_gt = {thr: float(norm.sf((thr * last_actual - delta) / sigma)) for thr in thresholds}
    return PriceChangeProbability(
        p_rise=p_rise,
        p_rise_gt=p_rise_gt,
        expected_change_pct=(delta / last_actual) if last_actual else 0.0,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && venv/bin/python -m pytest tests/test_price_change_probability.py -v`
Expected: 3 PASS. (If `scipy` not in venv, fall back to a pure-numpy erf implementation — check `venv/bin/python -c "import scipy.stats"`. If missing, replace `norm.cdf`/`norm.sf` with `0.5*(1+np.math.erf(x/np.sqrt(2)))`.)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/forecasting/price_change_probability.py backend/tests/test_price_change_probability.py
git commit -m "feat(forecast): price-change probability from conformal intervals"
```

---

### Task D3: Wire conformal + probability into scenarios, engine, schema

**Files:**
- Modify: `backend/app/services/forecasting/scenarios.py`
- Modify: `backend/app/services/forecasting/engine.py`
- Modify: `backend/app/schemas/market_dashboard.py`
- Modify: `frontend/src/pages/MarketDashboard/components/ForecastTrendChart.jsx`

**Interfaces:**
- `scenarios.generate()` gains optional `calibration: ConformalCalibration | None`. When provided, bull/bear use `calibration.interval()` at p10/p90 instead of 25/75 percentile.
- Engine passes `ConformalCalibration` (from `backtest_result.residuals_by_horizon`) into `scenarios.generate()` and computes `PriceChangeProbability` for the primary horizon, attaching to the explanation dict + response payload.
- Schema: `ForecastOverlayMeta` (or the forecast response) gains `prediction_interval: dict` (`{h: {lower, upper}}`) and `price_change_probability: dict` (`{h: {p_rise, p_rise_gt}}`).

- [ ] **Step 1: Write failing test for scenarios using conformal**

```python
# append to backend/tests/test_conformal.py
from app.services.forecasting.scenarios import generate
from app.services.forecasting.conformal import calibrate

def test_scenarios_use_conformal_when_provided():
    point = pd.Series(np.linspace(100, 110, 30))
    res = {7: list(np.random.default_rng(2).normal(0, 3, 100))}
    cal = calibrate(res, alpha=0.1)
    sc = generate(point, residuals=None, horizons=[7], calibration=cal)
    base = sc.horizons[7]["base"]
    bull = sc.horizons[7]["bull"]
    bear = sc.horizons[7]["bear"]
    assert (bull > base).all()
    assert (bear < base).all()
    assert sc.bounds_source == "conformal"
```

- [ ] **Step 2: Run to confirm fail**

Run: `cd backend && venv/bin/python -m pytest tests/test_conformal.py::test_scenarios_use_conformal_when_provided -v`
Expected: FAIL — `generate()` has no `calibration` param.

- [ ] **Step 3: Implement wiring**

1. `scenarios.py`: add `calibration: ConformalCalibration | None = None` param. When provided and `residuals` path would be used, instead use `calibration.interval(point_forecast, h)` → set bull=upper, bear=lower, `bounds_source="conformal"`.
2. `engine.py`: after backtest, build `cal = calibrate(backtest_result.residuals_by_horizon)`. Pass `cal` to `scenarios.generate()`. After scenarios, compute `pcp = price_change_probability.compute(point_forecast, last_actual=y.iloc[-1], calibration=cal, horizon=7)` (and 30). Attach to explanation dict as `probability` key, and to the horizon payload returned by the API.
3. `schemas/market_dashboard.py`: add `prediction_interval: dict | None = None` and `price_change_probability: dict | None = None` to the forecast response schema (find the existing overlay/forecast meta schema — search for `ForecastOverlayMeta` or the response model used by `get_forecast_base_batch_v2`).
4. `ForecastTrendChart.jsx`: in the forecast card, below the bull/base/bear legend, render a line: `"7d: {p_rise_rise_pct}% chance price rises"` using `data?.price_change_probability?.[7]?.p_rise`. Use the existing monochrome style; show green tint when p_rise>0.6, red when <0.4, neutral otherwise.

- [ ] **Step 4: Run tests + build**

Run: `cd backend && venv/bin/python -m pytest tests/test_conformal.py tests/test_price_change_probability.py tests/test_forecast_tool.py -v`
Expected: all PASS.
Run: `cd frontend && npm run build`
Expected: clean build.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/forecasting/scenarios.py backend/app/services/forecasting/engine.py backend/app/schemas/market_dashboard.py frontend/src/pages/MarketDashboard/components/ForecastTrendChart.jsx backend/tests/test_conformal.py
git commit -m "feat(forecast): wire conformal intervals + price-change probability into dashboard"
```

---

## Phase E — Directional + Decision Engine

**Why**: directional accuracy is 24-38% (below random). A dedicated classifier optimized for sign (not point MAPE) can find an edge where the point-forecast ensemble can't. Paired with Phase D probability, this gives buy/hold/sell.

### Task E1: Directional classifier

**Files:**
- Create: `backend/app/services/forecasting/directional_classifier.py`
- Test: `backend/tests/test_directional_classifier.py`

**Interfaces:**
- `DirectionalClassifier` — `.fit(X: pd.DataFrame, y_sign: pd.Series)` / `.predict_proba(X) -> np.ndarray` (P(up)).
- `build_features(y: pd.Series, exog: pd.DataFrame | None) -> pd.DataFrame` — returns lag returns, momentum (3/7/14d), volatility, calendar month, naphtha delta (if exog), domain elasticity flag.
- `backtest_directional(y, horizons=[7]) -> dict[str, float]` — walk-forward directional accuracy; returns `{model: accuracy}`. Must report `"no_edge"` if no model beats 0.55.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_directional_classifier.py
import numpy as np
import pandas as pd
import pytest
from app.services.forecasting.directional_classifier import (
    DirectionalClassifier, build_features, backtest_directional,
)


def _trend(n=200):
    rng = np.random.default_rng(7)
    t = np.arange(n, dtype=float)
    return pd.Series(100 + 0.3*t + rng.normal(0, 2, n), name="y")


def test_build_features_shape():
    y = _trend(100)
    X = build_features(y, exog=None)
    assert len(X) == 100
    assert "ret_lag1" in X.columns
    assert "momentum_7" in X.columns


def test_classifier_beats_random_on_trend():
    y = _trend(200)
    acc = backtest_directional(y, horizons=[7])
    # On a clean uptrend the classifier should beat 0.55
    assert acc.get("logistic", 0) >= 0.55 or acc.get("status") == "no_edge"


def test_no_edge_reported_on_noise():
    rng = np.random.default_rng(0)
    y = pd.Series(rng.normal(100, 1, 200), name="y")  # pure noise
    acc = backtest_directional(y, horizons=[7])
    assert acc.get("status") == "no_edge" or max(acc.get("logistic",0), acc.get("gbm",0)) < 0.6
```

- [ ] **Step 2: Run to confirm fail**

Run: `cd backend && venv/bin/python -m pytest tests/test_directional_classifier.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement**

```python
# backend/app/services/forecasting/directional_classifier.py
"""Directional (up/down) classifier — optimized for sign, not MAPE.

Two models: logistic regression + gradient boosting (sklearn). Walk-forward
backtest reports accuracy; if neither beats 0.55 we surface "no_edge" so the
dashboard honestly says "no directional signal" rather than a fake one.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import GradientBoostingClassifier

_EDGE_THRESHOLD = 0.55
_MIN_TRAIN = 60


def build_features(y: pd.Series, exog: pd.DataFrame | None = None) -> pd.DataFrame:
    df = pd.DataFrame({"y": y.values})
    df["ret_lag1"] = df["y"].pct_change(1)
    df["ret_lag3"] = df["y"].pct_change(3)
    df["momentum_7"] = df["y"].pct_change(7)
    df["momentum_14"] = df["y"].pct_change(14)
    df["vol_7"] = df["ret_lag1"].rolling(7).std()
    df["month"] = pd.Series(y.index).dt.month if hasattr(y.index, "dt") else 0
    if exog is not None and "naphtha_delta" in exog:
        df["naphtha_delta"] = exog["naphtha_delta"].values[:len(df)]
    return df.fillna(0.0)


class DirectionalClassifier:
    def __init__(self, kind: str = "logistic"):
        self.kind = kind
        self.model = LogisticRegression(max_iter=200) if kind == "logistic" else GradientBoostingClassifier(n_estimators=50)

    def fit(self, X, y_sign):
        self.model.fit(X, y_sign)
        return self

    def predict_proba(self, X):
        return self.model.predict_proba(X)[:, 1]


def backtest_directional(y, horizons=(7,)):
    y = y.dropna()
    n = len(y)
    if n < _MIN_TRAIN + 30:
        return {"status": "no_edge"}
    h = horizons[0]
    feats = build_features(y)
    future_sign = (y.shift(-h) > y).astype(int)
    results = {}
    for kind in ("logistic", "gbm"):
        accs = []
        for origin in range(_MIN_TRAIN, n - h, 14):
            X_tr = feats.iloc[:origin]
            y_tr = future_sign.iloc[:origin].dropna()
            if len(y_tr) < 30:
                continue
            X_te = feats.iloc[[origin]]
            clf = DirectionalClassifier(kind).fit(X_tr, y_tr)
            p = clf.predict_proba(X_te)[0]
            actual = future_sign.iloc[origin]
            accs.append(int(p >= 0.5) == int(actual))
        if accs:
            results[kind] = float(np.mean(accs))
    best = max(results.values()) if results else 0.0
    if best < _EDGE_THRESHOLD:
        results["status"] = "no_edge"
    return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && venv/bin/python -m pytest tests/test_directional_classifier.py -v`
Expected: 3 PASS. (If sklearn not installed: `venv/bin/pip install scikit-learn` — it should already be present for XGBoost features.)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/forecasting/directional_classifier.py backend/tests/test_directional_classifier.py
git commit -m "feat(forecast): directional classifier with no-edge honesty"
```

---

### Task E2: Decision engine (buy/hold/sell)

**Files:**
- Create: `backend/app/services/forecasting/decision_engine.py`
- Test: `backend/tests/test_decision_engine.py`

**Interfaces:**
- `recommend(p_rise: float, expected_change_pct: float, directional_acc: float | None, trust_tier: str) -> Decision` with fields `action: str` ("buy"/"hold"/"sell"/"watch"), `confidence: str` ("high"/"medium"/"low"), `rationale: str`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_decision_engine.py
import pytest
from app.services.forecasting.decision_engine import recommend, Decision


def test_buy_on_high_probability_high_trust():
    d = recommend(p_rise=0.78, expected_change_pct=0.06, directional_acc=0.62, trust_tier="high")
    assert d.action == "buy"
    assert d.confidence in ("high", "medium")


def test_hold_on_ambiguous():
    d = recommend(p_rise=0.52, expected_change_pct=0.01, directional_acc=0.50, trust_tier="medium")
    assert d.action == "hold"


def test_sell_on_low_probability():
    d = recommend(p_rise=0.20, expected_change_pct=-0.05, directional_acc=0.60, trust_tier="high")
    assert d.action == "sell"


def test_watch_when_no_edge():
    d = recommend(p_rise=0.55, expected_change_pct=0.02, directional_acc=None, trust_tier="low")
    assert d.action == "watch"
    assert "no directional" in d.rationale.lower() or "low confidence" in d.rationale.lower()
```

- [ ] **Step 2: Run to confirm fail**

Run: `cd backend && venv/bin/python -m pytest tests/test_decision_engine.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement**

```python
# backend/app/services/forecasting/decision_engine.py
"""Buy/hold/sell recommendation from probability + directional edge + trust."""
from __future__ import annotations
from dataclasses import dataclass

_BUY_THRESHOLD = 0.70
_SELL_THRESHOLD = 0.30
_EDGE_THRESHOLD = 0.55


@dataclass
class Decision:
    action: str
    confidence: str
    rationale: str


def recommend(p_rise, expected_change_pct, directional_acc, trust_tier):
    has_edge = directional_acc is not None and directional_acc >= _EDGE_THRESHOLD
    tier = (trust_tier or "low").lower()
    if not has_edge or tier == "low":
        return Decision("watch", "low",
                        "No reliable directional edge; insufficient confidence for a trade signal.")
    conf = "high" if tier == "high" and abs(p_rise - 0.5) > 0.25 else "medium"
    if p_rise >= _BUY_THRESHOLD and expected_change_pct > 0.03:
        return Decision("buy", conf, f"P(rise)={p_rise:.0%}, expected +{expected_change_pct:.1%}.")
    if p_rise <= _SELL_THRESHOLD and expected_change_pct < -0.03:
        return Decision("sell", conf, f"P(rise)={p_rise:.0%}, expected {expected_change_pct:.1%}.")
    return Decision("hold", conf, f"P(rise)={p_rise:.0%}; no actionable move.")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && venv/bin/python -m pytest tests/test_decision_engine.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/forecasting/decision_engine.py backend/tests/test_decision_engine.py
git commit -m "feat(forecast): buy/hold/sell decision engine with trust-gated honesty"
```

---

### Task E3: Wire classifier + decision into engine + schema + frontend

**Files:**
- Modify: `backend/app/services/forecasting/engine.py`
- Modify: `backend/app/schemas/market_dashboard.py`
- Modify: `frontend/src/pages/MarketDashboard/components/ForecastTrendChart.jsx`

- [ ] **Step 1: Wire engine**

In `engine.py compute_target()`: after Phase D probability is computed, call `dir_acc = backtest_directional(y, horizons=[7])` (cache per-target to avoid recompute every run — store on the target or in a module-level LRU). Then `decision = recommend(pcp.p_rise, pcp.expected_change_pct, dir_acc.get("logistic") if dir_acc.get("status")!="no_edge" else None, trust_tier)`. Attach `direction` (dir_acc summary) + `decision` to the explanation dict and response payload.

- [ ] **Step 2: Extend schema**

In `schemas/market_dashboard.py`: add `decision: dict | None = None` (`{action, confidence, rationale}`) and `directional_edge: dict | None = None` to the forecast response schema.

- [ ] **Step 3: Frontend decision badge**

In `ForecastTrendChart.jsx`: render a decision badge near the forecast title. Green "Buy" / gray "Hold" / red "Sell" / amber "Watch" using `data?.decision?.action`. Show `data?.decision?.rationale` as a tooltip.

- [ ] **Step 4: Run tests + build**

Run: `cd backend && venv/bin/python -m pytest tests/test_directional_classifier.py tests/test_decision_engine.py tests/test_forecast_tool.py -v`
Expected: all PASS.
Run: `cd frontend && npm run build`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/forecasting/engine.py backend/app/schemas/market_dashboard.py frontend/src/pages/MarketDashboard/components/ForecastTrendChart.jsx
git commit -m "feat(forecast): wire directional classifier + decision badge into dashboard"
```

---

## Phase F — Data Extension + Causal Enablement

**Why**: most products have 69-110 rows (13 months, weekly cadence) from `md_t_lz_price` alone. The EDIA MySQL warehouse has: (1) `lz_v_*_data` views extending isoprene/styrene back to 2025-03 with denser daily coverage, (2) `sale_erp_v_*_data` views with ERP transaction prices back to **2018** (6-8 years) usable as exogenous ML features. Plus the causal domain-signal chain (`FORECAST_DOMAIN_SIGNALS_ENABLED`) is currently OFF.

**Data map (verified 2026-08-04):**

| Table | Products | Span | Distinct dates | Use |
|-------|----------|------|----------------|-----|
| `md_t_lz_price` (current) | all 10 | 2025-06 → 2026-07 | 75-206 | Market quotation (primary) |
| `lz_v_异戊二烯_data` | isoprene | 2025-03 → 2026-07 | 305 | Extend market history +3mo |
| `lz_v_苯乙烯_data` | styrene | 2025-03 → 2026-07 | 304 | Extend market history +3mo |
| `sale_erp_v_异戊二烯_data` | isoprene | 2018-06 → 2026-07 | 531 | Exogenous ERP features |
| `sale_erp_v_双环戊二烯_data` | DCPD | 2018-06 → 2026-07 | 1381 | Exogenous ERP features |
| `sale_erp_v_碳五石油树脂_data` | c5_resin | 2018-07 → 2026-07 | 2365 | Exogenous ERP features |

**Caveat**: ERP transaction prices ≠ market quotations — never naively concatenate into the forecast target series. Use `lz_v_*` (also Longzhong market data) for target extension; use `sale_erp_v_*` only as exogenous features.

### Task F1: Multi-source history concatenation (md_t_lz + lz_v)

**Files:**
- Modify: `backend/app/services/forecasting/edia_source.py`
- Modify: `backend/app/services/forecasting/seed_ecisco_targets.py`
- Test: `backend/tests/test_edia_source_multi.py`

**Interfaces:**
- `EdiaMysqlDataSource.read_history_multi(sources: list[dict]) -> pd.DataFrame` — reads each source, concatenates, deduplicates by date (prefer the primary source on overlap), returns `df[ds, y]`.
- Seed targets for isoprene + styrene gain a `secondary_sources` list pointing at their `lz_v_*` view.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_edia_source_multi.py
import pandas as pd
import pytest
from datetime import datetime
from app.services.forecasting.edia_source import EdiaMysqlDataSource


class _FakeConn:
    def __init__(self, rows_by_table):
        self.rows_by_table = rows_by_table
    def execute(self, sql): self._sql = sql; return self
    def fetchall(self):
        # crude table detection
        for t, rows in self.rows_by_table.items():
            if t in self._sql: return rows
        return []
    def __enter__(self): return self
    def __exit__(self, *a): pass


class _FakeEngine:
    def __init__(self, rows_by_table): self._r = rows_by_table; self.last = None
    def connect(self): return _FakeConn(self._r)


def test_multi_source_concatenation_prefers_primary_on_overlap():
    primary = [("2026-01-02", 100.0), ("2026-01-03", 101.0)]
    secondary = [("2025-12-30", 98.0), ("2026-01-02", 999.0)]  # 999 should be dropped (overlap)
    eng = _FakeEngine({"md_t_lz_price": primary, "lz_v_x": secondary})
    src = EdiaMysqlDataSource(engine=eng)
    df = src.read_history_multi([
        {"table": "md_t_lz_price", "time_col": "FDATE", "measure": "FTAXPRICE", "where": "1=1"},
        {"table": "lz_v_x", "time_col": "biz_date", "measure": "tax_price", "where": "1=1"},
    ])
    # 3 distinct dates; 2026-01-02 keeps primary value 100, not 999
    assert len(df) == 3
    row_jan2 = df[df["ds"] == pd.Timestamp("2026-01-02")]
    assert row_jan2["y"].iloc[0] == 100.0
```

- [ ] **Step 2: Run to confirm fail**

Run: `cd backend && venv/bin/python -m pytest tests/test_edia_source_multi.py -v`
Expected: FAIL — `read_history_multi` doesn't exist.

- [ ] **Step 3: Implement**

In `edia_source.py`, add:
```python
def read_history_multi(self, sources: list[dict]) -> pd.DataFrame:
    """Read + concatenate multiple sources, dedup by date preferring earlier (primary) source."""
    frames = []
    for i, ds in enumerate(sources):
        try:
            df = self.read_history(ds)
        except RuntimeError:
            continue
        df["_src_rank"] = i  # primary (0) wins on overlap
        frames.append(df)
    if not frames:
        raise RuntimeError("All sources returned no rows")
    full = pd.concat(frames, ignore_index=True)
    full = full.sort_values(["ds", "_src_rank"]).drop_duplicates("ds", keep="first")
    return full[["ds", "y"]].sort_values("ds").reset_index(drop=True)
```
In `seed_ecisco_targets.py`: for isoprene + styrene targets, add `"secondary_sources": [{"table": "lz_v_异戊二烯_data", "time_col": "biz_date(业务日期)", "measure": "tax_price(含税单价)", "where": "1=1"}]` (and the styrene equivalent). Engine: if `target.datasource` has `secondary_sources`, call `read_history_multi([datasource, *secondary_sources])` instead of `read_history(datasource)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && venv/bin/python -m pytest tests/test_edia_source_multi.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/forecasting/edia_source.py backend/app/services/forecasting/seed_ecisco_targets.py backend/app/services/forecasting/engine.py backend/tests/test_edia_source_multi.py
git commit -m "feat(forecast): multi-source history (md_t_lz + lz_v) for isoprene/styrene"
```

---

### Task F2: ERP exogenous feature source (sale_erp_v 2018+ history)

**Files:**
- Create: `backend/app/services/forecasting/erp_feature_source.py`
- Modify: `backend/app/services/forecasting/features/feature_builder.py`
- Test: `backend/tests/test_erp_feature_source.py`

**Interfaces:**
- `ErpFeatureSource.read_erp_lags(product_key: str, lookback_days: int = 365) -> pd.DataFrame` — daily-aggregated ERP price + lag features (ret_7, ret_30, vol_30) aligned to the market series dates.
- `feature_builder.build()` gains optional `erp_features: pd.DataFrame | None` — merges ERP lag columns into the XGBoost feature matrix.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_erp_feature_source.py
import pandas as pd, pytest
from app.services.forecasting.erp_feature_source import ErpFeatureSource


class _FakeEngine:
    def connect(self):
        class C:
            def execute(self, sql): self.sql=sql; return self
            def fetchall(self): return [("2026-01-0"+str(i), 100.0+i) for i in range(1,8)]
            def __enter__(self): return self
            def __exit__(self,*a): pass
        return C()


def test_erp_lags_aligned_to_market_dates():
    src = ErpFeatureSource(engine=_FakeEngine())
    market_dates = pd.date_range("2026-01-01", periods=7, freq="D")
    df = src.read_erp_lags("ecisco.isoprene", market_dates=market_dates)
    assert "erp_ret_7" in df.columns
    assert len(df) == 7
```

- [ ] **Step 2: Run to confirm fail** → implement `erp_feature_source.py` (query `sale_erp_v_*` daily-aggregated, compute ret_7/ret_30/vol_30, reindex to market_dates forward-fill) → run pass.

- [ ] **Step 3: Wire into feature_builder** — add `erp_features` param to `build()`; left-merge on date.

- [ ] **Step 4: Run tests** → commit.

```bash
git add backend/app/services/forecasting/erp_feature_source.py backend/app/services/forecasting/features/feature_builder.py backend/tests/test_erp_feature_source.py
git commit -m "feat(forecast): ERP exogenous lag features (2018+ history) for XGBoost"
```

---

### Task F3: Enable domain signals + A/B backtest validation

**Files:**
- Modify: `.env` (set `FORECAST_DOMAIN_SIGNALS_ENABLED=true`)
- Modify: `backend/tests/walk_forward_backtest.py` (add `--ab-domain` mode)

- [ ] **Step 1: Add A/B harness for domain signals**

In `walk_forward_backtest.py`, add a mode that runs backtest with `FORECAST_DOMAIN_SIGNALS_ENABLED=false` vs `true` (monkeypatch the config flag) and prints MAPE delta per product. If domain signals help most products (lower MAPE), keep enabled; if they hurt, report and leave disabled.

- [ ] **Step 2: Run the A/B**

Run: `cd backend && venv/bin/python tests/walk_forward_backtest.py --ab-domain 2>&1 | tail -30`
Capture the table. Decide enable/disable based on results.

- [ ] **Step 3: Set flag + restart**

If A/B favorable: set `FORECAST_DOMAIN_SIGNALS_ENABLED=true` in `.env`, `docker compose restart backend`.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/walk_forward_backtest.py
git commit -m "test(forecast): A/B harness for domain-signal causal chain"
```

---

### Task F4: Deploy + end-to-end verification

- [ ] **Step 1: Run full test suite (individually per RAM limits)**

```bash
cd backend && venv/bin/python -m pytest tests/test_walk_forward_backtest_unit.py tests/test_per_horizon_ensemble.py tests/test_model_pool_gating.py tests/test_conformal.py tests/test_price_change_probability.py tests/test_directional_classifier.py tests/test_decision_engine.py tests/test_edia_source_multi.py tests/test_erp_feature_source.py tests/test_forecast_tool.py -v
```
Expected: all PASS.

- [ ] **Step 2: Restart backend + verify API**

```bash
docker compose restart backend
# login + hit forecast endpoint
curl -s -X POST http://localhost:5002/api/apps/local-zhanlu-app/auth/login -H 'Content-Type: application/json' -d '{"email":"admin@zhanlu.dev","password":"admin123"}'
# fetch forecast for isoprene, confirm prediction_interval + price_change_probability + decision present
```

- [ ] **Step 3: Frontend build + preview**

```bash
cd frontend && npm run build
```
Verify the dashboard shows: calibrated bull/bear bands, "% chance price rises" line, and Buy/Hold/Sell/Watch badge.

- [ ] **Step 4: Commit any final wiring**

```bash
git add -A && git commit -m "feat(forecast): accuracy overhaul Phase C-F complete"
```

---

## Verification Matrix

| Phase | Metric | Before | Target |
|-------|--------|--------|--------|
| C | 7d MAPE (aggregate) | 11.4% | <9% |
| C | 30d MAPE (aggregate) | 23.5% | <18% |
| C | XGBoost inf% products | 2 (C9, Styrene) | 0 |
| C | Reported MAPE honesty | in-sample (optimistic) | walk-forward (true OOS) |
| D | Prediction interval coverage | uncalibrated (±sigma) | ~90% empirical |
| D | Price-change probability | none | per-horizon p_rise + thresholds |
| E | Directional accuracy | 24-38% (below random) | >55% on products with edge; honest "no edge" otherwise |
| E | Decision signal | none | buy/hold/sell/watch badge |
| F | Isoprene/Styrene history | 88/78 rows (from 2025-06) | ~300+ rows (from 2025-03) |
| F | ERP exogenous features | none | 6-8yr lag features for XGBoost |
| F | Domain causal chain | disabled | enabled (if A/B favorable) |

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Per-horizon weights unstable on short series | Fall back to flat weights when n_folds < 3 |
| Conformal coverage poor on non-stationary series (regime shifts) | Report empirical coverage in backtest; cap interval width at ±25% of last actual |
| Directional classifier overfits on 13-month data | Require 0.55 walk-forward accuracy; surface "no edge" honestly; prefer logistic (low-variance) over GBM on short series |
| ERP features leak future info | Strict lag-only features (ret_7 uses only past 7d); reindex to market dates with forward-fill |
| Domain signals hurt some products | Per-product A/B; allow per-product flag override if needed |
| scipy not installed | Fallback to numpy erf in `price_change_probability.py` |
| sklearn not installed | Should be present (XGBoost deps); if not, `pip install scikit-learn` |

## Out of Scope (Future)

- HuggingFace foundation models (Chronos-Bolt/TimesFM) — deferred; revisit for cold-start SKUs once data exhausted
- Probabilistic ensemble (Bayesian model averaging) — conformal on point ensemble is sufficient
- Per-product model selection meta-learner — per-horizon weights already capture most of the gain
- LSTM / deep learning — deliberately not ported from EDIA (heavy, marginal gain)
- Automated trading execution — decision engine is advisory only

## Related Documents

- `docs/superpowers/plans/2026-08-04-sku-level-forecasting.md` (Phase A+B — done)
- `docs/superpowers/plans/2026-08-04-forecast-intelligence-upgrade.md` (Phase 4-7 — done)
- `backend/tests/walk_forward_backtest.py` (proven E2E backtest script — source of truth for current accuracy)
