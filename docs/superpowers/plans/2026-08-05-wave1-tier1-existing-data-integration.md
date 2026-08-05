# Wave 1 — Tier1: Existing-Data Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect Wave 0's three already-built but un-wired components — `ErpVolumeLoader`, `demand_signal`, and `decision_roi` — into the actual forecast pipeline and AI Brief: ERP sales volume as XGBoost exogenous features, demand signal rendered into the 供需研判 section, and decision thresholds calibrated from historical ROI. All flag-gated, all default OFF, zero regression.

**Architecture:** Wave 0 built the data loaders, pure metric functions, and ROI replay logic. Wave 1 wires them in: `feature_builder.py` gains an optional `volume_df` parameter that adds `erp_volume_lag{N}` columns, `engine.py` Step 5 optionally instantiates `ErpVolumeLoader`, `analyst/service.py` optionally loads volume+dispersion and computes demand signals for the evidence pack, `template_brief._supply_demand()` renders them as Chinese text, and `decision_engine.py` thresholds become overridable via config with a `threshold_calibrator.py` that wraps `grid_search_thresholds` with safety guardrails and a calibrate API endpoint. All behind three new `.env` flags, all default OFF.

**Tech Stack:** Python 3.11, SQLAlchemy 2.0, pandas, xgboost, pytest (`backend/venv/bin/python -m pytest`).

## Global Constraints

- **Backend venv**: run all tests with `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest <path> -v`. The system has no pytest.
- **Zero regression**: all new behavior is flag-gated and default OFF. Existing tests must pass unchanged.
- **No future leakage**: ERP volume lag windows must not peek ahead. Lag 1 means `shift(1)`, no forward-looking.
- **ERP volume is exogenous, NOT the forecast target**: the forecast target remains price. Volume is a demand-proxy feature.
- **Threshold calibration is advisory only**: grid_search results are returned via API for human review; they do NOT auto-override `decision_engine.py` constants. Manual override via `.env` or config table.
- **Schema management**: no new tables (Wave 0 already created `forecast_decision_logs` + `forecast_event_impacts`). No Alembic migration.
- **Chinese market colors**: 涨=red, 跌=green.
- **MySQL engine**: `from app.core.mysql_db import get_mysql_engine`. Loaders tolerate `None` engine → empty DataFrame.
- **Docker**: after touching service code → `docker restart zhanlu-backend`. **`.env` flag changes need `docker compose up -d --force-recreate backend`** (env_file baked at creation). Port 5002.
- **Test isolation**: mock/patched engines for loader tests; use SQLite in-memory for ORM tests.
- **Feature engineering convention**: lag column names are `erp_volume_lag1` to `erp_volume_lag{N}` matching the existing `feedstock_*_lag{N}` pattern in `feature_builder.py`.

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `backend/app/services/forecasting/features/feature_builder.py` | Add `volume_df` param → `erp_volume_lag{N}` columns in X_train/X_future | Modify |
| `backend/app/services/forecasting/engine.py` | Step 5: optionally instantiate `ErpVolumeLoader`, pass `volume_df` to `build_features` | Modify |
| `backend/app/services/forecasting/analyst/service.py` | Load volume+dispersion, compute demand signals, pass to `build_pack` | Modify |
| `backend/app/services/forecasting/analyst/template_brief.py` | `_supply_demand()` reads `p.get("demand")` + `p.get("supplier_ladder")`, renders Chinese text | Modify |
| `backend/app/services/forecasting/decision_engine.py` | Thresholds overridable from config/env | Modify |
| `backend/app/services/forecasting/features/threshold_calibrator.py` | Wrap `grid_search_thresholds` + safety guardrails, produce calibration report | Create |
| `backend/app/routers/forecast_ops.py` | Add `POST /calibrate-thresholds` admin endpoint | Modify |
| `.env` | Add 3 flags: `FORECAST_ERP_VOLUME_EXOG_ENABLED`, `FORECAST_DEMAND_SIGNAL_ENABLED`, `FORECAST_THRESHOLD_CALIBRATION_ENABLED` | Modify |
| `backend/tests/test_forecasting/test_wave1_feature_builder_volume.py` | Test `build_features` with `volume_df` param | Create |
| `backend/tests/test_forecasting/test_wave1_analyst_demand_signal.py` | Test service.py demand signal integration | Create |
| `backend/tests/test_forecasting/test_wave1_threshold_calibrator.py` | Test calibrator with mock decision logs | Create |
| `backend/tests/test_forecasting/test_wave1_template_demand_render.py` | Test `_supply_demand()` renders demand/supplier text | Create |

---

### Task 1: ERP Volume → XGBoost Exogenous Features

**Files:**
- Modify: `backend/app/services/forecasting/features/feature_builder.py`
- Modify: `backend/app/services/forecasting/engine.py`
- Test: `backend/tests/test_forecasting/test_wave1_feature_builder_volume.py`

**Interfaces:**
- `build_features()` gains optional param `volume_df: pd.DataFrame | None = None`. If provided, adds `erp_volume_lag{1..N}` columns to both X_train and X_future. Lag N default = 7 (7-day rolling demand proxy).
- `engine.py` Step 5 (L273-321): reads `FORECAST_ERP_VOLUME_EXOG_ENABLED` flag. If true, instantiates `ErpVolumeLoader(product_id, lookback_days=365)`, calls `.load()`, passes result as `volume_df` to `build_features`.
- Flag: `FORECAST_ERP_VOLUME_EXOG_ENABLED` — default `false`.
- **Key constraint**: volume_df must cover `y_dates` minus max_lag days. If volume data is insufficient (less than 7 days), skip and log a warning — don't crash.

- [ ] **Step 1: feature_builder.py — add volume_df parameter**

In `build_features()`, add `volume_df: pd.DataFrame | None = None` param. If not None:
- For X_train: align volume with each y_date, create `erp_volume_lag{1..7}` using `shift(1..7)`.
- For X_future (when `cascade_forecasts` is available): extend volume by carrying forward recent values, create same lag columns.
- Add `erp_volume_lag{N}` to `feature_names` list.
- Drop NaN rows from X_train that result from lag creation (mirrors existing `dropna` in the function).
- If volume_df is too short, log warning and skip volume features.

- [ ] **Step 2: engine.py — flag-gated ErpVolumeLoader in Step 5**

In `compute_target()` Step 5 (after `event_loader = EventFlagLoader(...)`):
```python
volume_df = None
if os.environ.get("FORECAST_ERP_VOLUME_EXOG_ENABLED", "false").lower() == "true":
    try:
        vol_loader = ErpVolumeLoader(target.product_id, lookback_days=365, org_id=target.org_id)
        volume_df = vol_loader.load()
        logger.info(f"ERP volume loaded for {target.product_key}: {len(volume_df)} days")
    except Exception as e:
        logger.warning(f"ERP volume load failed for {target.product_key}: {e}")
        volume_df = None
```
Then pass `volume_df=volume_df` to `build_features`.

- [ ] **Step 3: Tests**

`test_wave1_feature_builder_volume.py`:
- `test_build_features_with_volume_adds_lag_columns`: synthetic volume + y data → verify X_train has `erp_volume_lag1` through `erp_volume_lag7`.
- `test_build_features_without_volume_unchanged`: call without volume_df → same output as before (zero regression).
- `test_build_features_volume_insufficient`: volume_df with 3 rows → should warn and skip (no new columns).
- `test_engine_flag_off_does_not_load_volume`: verify engine does NOT instantiate ErpVolumeLoader when flag is off.

---

### Task 2: Demand Signal → AI Brief 供需研判 Section

**Files:**
- Modify: `backend/app/services/forecasting/analyst/service.py`
- Modify: `backend/app/services/forecasting/analyst/template_brief.py`
- Test: `backend/tests/test_forecasting/test_wave1_analyst_demand_signal.py`
- Test: `backend/tests/test_forecasting/test_wave1_template_demand_render.py`

**Interfaces:**
- `service.py` `_build_pack_for()`: reads `FORECAST_DEMAND_SIGNAL_ENABLED`. If true, loads `ErpVolumeLoader` + `SupplierDispersionLoader`, computes `compute_demand_signal(volume_df, price_df)` and `compute_supplier_ladder_signal(dispersion_df)`, passes both to `build_pack(demand_signal=..., supplier_ladder=...)`.
- `template_brief._supply_demand(p)`: reads `p.get("demand")` and `p.get("supplier_ladder")`, renders Chinese 供需研判 text:
  - 需求: rolling 4-week volume trend (up/down/flat), YoY % change, volume-price divergence signal if present.
  - 供给: supplier dispersion trend (widening/narrowing spread), average supplier count.
  - Format: three paragraphs max, Chinese prose, data-driven.
- Flag: `FORECAST_DEMAND_SIGNAL_ENABLED` — default `false`.

- [ ] **Step 1: service.py — load and compute demand signals**

In `_build_pack_for(day, run)` after `price_data` computed:
```python
demand_signal = None
supplier_ladder = None
if os.environ.get("FORECAST_DEMAND_SIGNAL_ENABLED", "false").lower() == "true":
    try:
        vol_loader = ErpVolumeLoader(product_id, lookback_days=365, org_id=org_id)
        volume_df = vol_loader.load()
        if not volume_df.empty and len(volume_df) >= 28:
            demand_signal = compute_demand_signal(volume_df, price_df=price_series,
                                                   product_id=product_key,
                                                   rolling_window=28, yoy_window=364)
    except Exception as e:
        logger.warning(f"Demand signal compute failed: {e}")
    try:
        disp_loader = SupplierDispersionLoader(product_id, lookback_days=90, org_id=org_id)
        dispersion_df = disp_loader.load()
        if not dispersion_df.empty:
            supplier_ladder = compute_supplier_ladder_signal(dispersion_df,
                                                              product_id=product_key,
                                                              recent_days=30)
    except Exception as e:
        logger.warning(f"Supplier ladder compute failed: {e}")
```
Pass to `build_pack(..., demand_signal=demand_signal, supplier_ladder=supplier_ladder)`.

- [ ] **Step 2: template_brief.py — render demand/supplier in _supply_demand()**

In `_supply_demand(p)`, after existing `seasonal`/`drivers`/`agreement` rendering, add:
```python
# 供需研判 (from Wave 1 demand signals)
demand = p.get("demand")
supplier = p.get("supplier_ladder")
if demand and isinstance(demand, dict) and demand.get("has_sufficient_data"):
    trend_text = {"up": "需求上行", "down": "需求收缩", "flat": "需求平稳"}.get(
        demand.get("demand_trend", ""), ""
    )
    lines = []
    lines.append(f"近4周销量滚动均值 {demand.get('rolling_4wk_vol', 0):.0f}，{trend_text}。")
    yoy = demand.get("yoy_change_pct")
    if yoy is not None:
        direction = "同比增长" if yoy >= 0 else "同比下降"
        lines.append(f"{direction} {abs(yoy):.1f}%。")
    div = demand.get("vol_price_divergence")
    if div is not None and abs(div) > 0.1:
        direction_text = "量价背离" if div > 0 else "量价收敛"
        lines.append(f"{direction_text}信号显著（{div:+.2f}）。")
    if supplier and isinstance(supplier, dict) and supplier.get("has_data"):
        lines.append(
            f"供应商价差均值 {supplier.get('avg_spread', 0):.0f}，"
            f"趋势{supplier.get('spread_trend', '')}（近{supplier.get('recent_days', 30)}天）。"
        )
    # Append after existing content
    ...
```
The `_supply_demand` function currently returns a string — append the new lines at the end.

- [ ] **Step 3: Tests**

`test_wave1_analyst_demand_signal.py`:
- `test_build_pack_with_demand_signal_enabled`: patch loaders + flag ON → verify `build_pack` called with non-None `demand_signal`/`supplier_ladder`.
- `test_build_pack_with_flag_off_no_signal`: flag OFF → `build_pack` called with None for both (zero regression).
- `test_build_pack_handles_loader_failure`: loader raises → demand signals are None, function still returns valid pack.

`test_wave1_template_demand_render.py`:
- `test_supply_demand_renders_demand_text`: mock pack with demand data → rendered text contains "需求上行"/"需求收缩"/"需求平稳".
- `test_supply_demand_renders_yoy`: pack with `yoy_change_pct=12.5` → output contains "同比增长 12.5%".
- `test_supply_demand_renders_divergence`: pack with `vol_price_divergence=0.3` → output contains "量价背离".
- `test_supply_demand_no_demand_data_unchanged`: empty demand → existing seasonal/drivers/agreement still rendered normally.
- `test_supply_demand_with_supplier_ladder`: pack with supplier_ladder data → output contains "供应商价差".

---

### Task 3: Threshold Calibration from Decision History

**Files:**
- Modify: `backend/app/services/forecasting/decision_engine.py`
- Create: `backend/app/services/forecasting/features/threshold_calibrator.py`
- Modify: `backend/app/routers/forecast_ops.py`
- Modify: `backend/app/services/forecasting/engine.py` (Step 9 — log decisions for future calibration)
- Test: `backend/tests/test_forecasting/test_wave1_threshold_calibrator.py`

**Interfaces:**
- `decision_engine.recommend()`: reads threshold values from `os.environ` with current hardcoded defaults as fallback.
- `threshold_calibrator.py`: `calibrate_thresholds(db, product_key=None, days=90, buy_range=None, sell_range=None, min_change=0.03)` → `CalibrationReport` dataclass. Wraps `grid_search_thresholds` with safety guardrails (min sample size ≥ 10, ROI sanity checks, buy/sell thresholds must maintain gap ≥ 0.15).
- `forecast_ops.py`: `POST /calibrate-thresholds` (admin-only). Body: `{"product_key": "TN450", "days": 90}`. Returns top-3 grid search results + recommendation + safety checks.
- `engine.py` Step 9: if `FORECAST_THRESHOLD_CALIBRATION_ENABLED=true`, gate a decision-logger call so future decisions are recorded for calibration.
- Flag: `FORECAST_THRESHOLD_CALIBRATION_ENABLED` — default `false`.

- [ ] **Step 1: decision_engine.py — configurable thresholds**

Replace hardcoded constants with `os.environ.get` with fallbacks:
```python
_BUY_THRESHOLD = float(os.environ.get("FORECAST_BUY_THRESHOLD", "0.70"))
_SELL_THRESHOLD = float(os.environ.get("FORECAST_SELL_THRESHOLD", "0.30"))
_BUY_MIN_CHANGE = float(os.environ.get("FORECAST_BUY_MIN_CHANGE", "0.03"))
_SELL_MIN_CHANGE = float(os.environ.get("FORECAST_SELL_MIN_CHANGE", "-0.03"))
_EDGE_THRESHOLD = float(os.environ.get("FORECAST_EDGE_THRESHOLD", "0.55"))
```
Values unchanged — only source changed to env-overridable.

- [ ] **Step 2: threshold_calibrator.py — calibration service**

```python
@dataclass
class CalibrationReport:
    product_key: str
    sample_size: int
    date_range: tuple  # (min_date, max_date)
    top_results: list[dict]  # top-3 grid search results sorted by ROI
    recommendation: dict | None  # best result if safety checks pass
    safety_checks: dict  # {"min_samples": bool, "gap_ok": bool, "roi_positive": bool}
    warnings: list[str]

def calibrate_thresholds(db: Session, product_key=None, days=90,
                         buy_range=None, sell_range=None, min_change=0.03) -> CalibrationReport:
    """Query decision logs, grid-search thresholds, return calibrated values with guardrails."""
```

Implementation:
1. Query `ForecastDecisionLog` with `status='scored'`, `decision_roi_pct IS NOT NULL`, filter by `product_key` (optional) and `as_of_date >= (now - days)`.
2. Convert to list of dicts with keys: `action, realized_price_t, realized_price_th, p_rise, expected_change_pct`.
3. If sample_size < 10: return report with all safety checks failed, warning about insufficient data.
4. Call `grid_search_thresholds(logs, buy_range=buy_range, sell_range=sell_range, min_change=min_change)`.
5. Sort by `roi_pct` descending, take top 3.
6. Safety checks:
   - `buy_threshold` - `sell_threshold` ≥ 0.15 (gap check)
   - Best ROI > 0 (positive ROI)
   - All checks marked in `safety_checks` dict.
7. Return `CalibrationReport`.

- [ ] **Step 3: forecast_ops.py — calibrate API endpoint**

Add `POST /calibrate-thresholds`:
```python
@forecast_ops_router.post("/calibrate-thresholds")
async def calibrate_thresholds(
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_admin_user),  # admin-only
):
    """Run grid-search calibration on historical decision logs. Results are advisory only."""
    product_key = body.get("product_key")
    days = body.get("days", 90)
    report = calibrate_thresholds(db, product_key=product_key, days=days)
    return {
        "product_key": report.product_key,
        "sample_size": report.sample_size,
        "date_range": report.date_range,
        "top_results": report.top_results,
        "recommendation": report.recommendation,
        "safety_checks": report.safety_checks,
        "warnings": report.warnings,
        "note": "Thresholds are advisory only. Apply manually via .env FORECAST_BUY_THRESHOLD / FORECAST_SELL_THRESHOLD / FORECAST_EDGE_THRESHOLD after review."
    }
```

- [ ] **Step 4: engine.py Step 9 — log decisions when calibration enabled**

If `FORECAST_THRESHOLD_CALIBRATION_ENABLED=true`, after `decision_engine.recommend()` call, optionally call `log_decision()` to record the decision for future calibration. This can be a simple gate — the actual logging infrastructure already exists from Wave 0 F2.

- [ ] **Step 5: Tests**

`test_wave1_threshold_calibrator.py`:
- `test_calibrate_with_insufficient_data`: < 10 scored logs → returns warning, all safety checks fail.
- `test_calibrate_with_sufficient_data`: ≥ 10 scored logs with known outcomes → returns top-3 results, best ROI > 0 passes safety check.
- `test_calibrate_gap_check_fails`: grid search finds buy=0.55, sell=0.50 → gap=0.05 < 0.15 → safety_checks.gap_ok = False.
- `test_calibrate_with_product_filter`: filter by product_key → only that product's logs used.
- `test_decision_engine_reads_env_thresholds`: set env var FORECAST_BUY_THRESHOLD=0.80 → recommend() uses 0.80.

---

### Task 4: Flags, Integration Verification, and Full Regression

**Files:**
- Modify: `.env`
- Test: all existing test files (regression) + new test files (Tasks 1-3)

**Goal:** Write the three new flags to `.env` (all default OFF), run the full regression suite to verify zero breakage, run all new Wave 1 tests, and verify that with all flags OFF the system behaves identically to pre-Wave-1 state.

- [ ] **Step 1: Add flags to .env**

Append to `backend/.env`:
```
# Wave 1: Tier1 Existing-Data Integration (all default off)
FORECAST_ERP_VOLUME_EXOG_ENABLED=false
FORECAST_DEMAND_SIGNAL_ENABLED=false
FORECAST_THRESHOLD_CALIBRATION_ENABLED=false
```

- [ ] **Step 2: Run existing regression tests**

```bash
cd /home/ysk2025/zhanlu_7_30/backend
venv/bin/python -m pytest tests/test_forecasting/ -v --tb=short
```
All pre-Wave-1 tests must pass (33 from Wave 0 + baseline).
Key files to verify: `test_exogenous_loaders.py`, `test_demand_signal.py`, `test_decision_roi.py`, `test_forecast_analyst_pack.py`, `test_forecast_analyst_template.py`, `test_decision_logger.py`, `test_decision_log_model.py`.

- [ ] **Step 3: Run new Wave 1 tests**

```bash
cd /home/ysk2025/zhanlu_7_30/backend
venv/bin/python -m pytest tests/test_forecasting/test_wave1_feature_builder_volume.py tests/test_forecasting/test_wave1_analyst_demand_signal.py tests/test_forecasting/test_wave1_template_demand_render.py tests/test_forecasting/test_wave1_threshold_calibrator.py -v --tb=short
```

- [ ] **Step 4: Verify flag-gate behavior**

With all flags OFF (default `.env` state):
- `engine.py` does NOT instantiate `ErpVolumeLoader`
- `service.py` does NOT load demand signals
- `decision_engine.py` uses default hardcoded thresholds (0.70/0.30)
- All existing tests pass unchanged

- [ ] **Step 5: Docker restart**

After all code changes:
```bash
docker restart zhanlu-backend
# Verify backend starts: curl http://localhost:5002/api/health
```

Flag changes to `.env` require `docker compose up -d --force-recreate backend`.

---

## Task Dependency Graph

```
Task 1 (erp-volume-exog) ──┐
                           ├──> Task 4 (flags + regression)
Task 2 (demand-signal-brief) ┤
                           │
Task 3 (threshold-calibration) ──┘ (depends on Task 1 for logging gate in engine.py)
```

Tasks 1 and 2 are independent and can be dispatched in parallel design, but SDD processes them sequentially. Task 3 references engine.py Step 9 (same area as Task 1) so run after Task 1 completes.
