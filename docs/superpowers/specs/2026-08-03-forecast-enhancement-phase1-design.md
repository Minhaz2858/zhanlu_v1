# Industrial Market-Price Forecast Enhancement — Phase 1 Design Spec

**Date:** 2026-08-03
**Author:** CodeBuddy (pair programming with user, via brainstorming skill)
**Status:** Approved (user said "ok do it please")
**Scope:** Enhance the existing `ForecastEngine` with exogenous feature engineering, anomaly-robust preprocessing, value-chain coherence, and continuous learning + interpretability. Targets the 12 nightly-run Acme BI dashboard products. Goal: lift the honesty-gate pass rate and give operators a defensible explanation of every forecast.

---

## 1. Goal

The nightly forecast engine already runs for all 12 dashboard products, but for most of them the **honesty gate triggers** — i.e. the 7-model ensemble's backtest MAPE is worse than `seasonal_naive`, so the engine publishes the naive forecast instead. This is expected: daily chemical prices are near a random walk with weekly seasonality *when modeled endogenously*. The fix is to feed the engine the external drivers it currently ignores.

Phase 1 delivers four capabilities the user selected:

1. **Exogenous feature engineering** — extend XGBoost with feedstock prices/spreads, FX, and market-event flags. Use a **forecast cascade** (crude → product_c → cracked sample product → derivatives → downstream) so each downstream product's future-step exogenous values come from that feedstock's own nightly forecast.
2. **Anomaly-robust preprocessing** — detect spikes, level shifts, and stale data before modeling; winsorize training features only.
3. **Value-chain coherence** (interpreted "hierarchical reconciliation") — cascade ordering + spread guardrails + directional consistency flags. *Not* literal HTS reconciliation, because the crude→product_c→sample product chain is a production chain with yield rates, not an additive disaggregation.
4. **Continuous learning + interpretability** — per-product rolling accuracy tracker with drift detection + adaptive ensemble reweighting + driver attribution + NL explanation on every `ForecastRun`.

### Decisions (from brainstorming Q&A)

| Question | Decision |
|---|---|
| Forecasting domain | **Both, phased** — Phase 1 = market-price (this spec); Phase 2 = plant-process ingestion (separate spec later) |
| Phase 1 must-haves | **All four**: exogenous features, anomaly-robust preprocessing, hierarchical reconciliation, continuous learning + interpretability |
| Exogenous-feature strategy for the horizon | **A: Forecast cascade** — feed downstream products the forecasted feedstock price for future steps. Models real cost pass-through; reuses nightly feedstock runs. Honesty gate dampens cascade error. |

## 2. Non-goals

- **Plant-process / industrial-IoT forecasting** (OPC UA/DCS/SCADA connectors, sensor streaming, equipment-degradation models) — deferred to Phase 2, separate spec.
- **New endogenous model families** (Prophet, TBATS, neural nets) — keep the existing 7-model pool; only XGBoost consumes exogenous features in Phase 1. ARIMA/ETS/STL/naive stay endogenous as robust baselines.
- **Literal HTS reconciliation** (Hyndman OLS/MinT) — wrong fit for a production chain with yield rates. Replaced by value-chain coherence (§5).
- **SHAP dependency** — driver attribution uses XGBoost `feature_importances_` + a contribution delta. Lighter, faster, sufficient for operators. SHAP can be added later if operators want deeper attribution.
- **Quantile forecast intervals** — current scenarios (base/bull/bear) stay. Adding proper quantile regression is a Phase 1.5 candidate, not in this spec.
- **Frontend changes** — explanations surface via the existing `forecast_get` tool and the dashboard's existing forecast card. A dedicated operator-facing explanation panel is a follow-up if the NL text proves valuable.

## 3. Current state (verified)

- **`ForecastEngine`** (`backend/app/services/forecasting/engine.py`): 7-model ensemble pipeline — discovery → fetch series → quality score → models → ensemble blend → guard → scenarios → cache. Reads/writes `ForecastTarget` + `ForecastRun` + `ForecastAccuracyLog`.
- **Models** (`backend/app/services/forecasting/models/`): `naive_last`, `seasonal_naive`, `ets`, `arima`, `stl`, `mean_reversion`, `xgboost_reg`. Only `xgboost_reg` could consume exogenous features today, but doesn't — its features (`xgboost_reg.py:124-151`) are purely endogenous: 14 lags, seasonal lag, day-of-week sin/cos, rolling mean/std.
- **Ensemble** (`ensemble.py`): softmax over negative backtest MAPE, 0.05 floor weight per model.
- **Honesty gate** (`guard.py`): if ensemble MAPE ≥ seasonal_naive MAPE, publish the naive forecast instead and set `below_naive_baseline=True`. On the last nightly run, this triggered for most of the 12 products.
- **Quality scoring** (`quality.py`): 6-factor forecastability grade (history length, missing ratio, IQR outliers, ADF stationarity, STL seasonality, frequency regularity). Scores but does not *clean* the series.
- **Backtest** (`backtest.py`): expanding-window, up to 10 folds.
- **Acme MySQL source** (`edia_source.py`): `EdiaMysqlDataSource.read_history()` reads daily prices from `md_market_price`. LIVE Longzhong sync, max FDATE 2026-07-30. 12 `ForecastTarget` rows seeded by `seed_acme_targets.py` (10 `md_market_price` + 2 ERP fallback for `feedstock_a` / `product_a`).
- **Nightly batch** (`compute_all`): runs via `automation_runtime_agent` at 2 AM (cron `0 2 * * *`), processes all active targets serially. Output read by `/api/market-dashboard/forecast-base-batch?day=7` (v2 reader in `market_dashboard_service.py`).
- **Product hierarchy** (`backend/app/services/knowledge_graph/models.py`): `ProductType` enum (CRUDE → FEEDSTOCK → sample_MIX → PRODUCT_A_DERIVATIVE → DOWNSTREAM). `Relationship` edges with `PRODUCES`/`CONSUMES`/`SUBSTITUTE` types and a `yield_rate` field (e.g. "1 ton Product_C → 0.15 ton byproduct"). `KnowledgeGraph.upstream_of(product)` returns the feedstock chain. This single structure powers **both** exogenous-feature selection **and** value-chain coherence.
- **Market events** (`backend/app/services/intelligence/event_types.py`): `ExtractedEvent` is richly structured — `event_type` (plant_outage, opec_decision, currency_move, sanctions, force_majeure, demand_surge, …), `affected_commodities`, `direction` (bullish/bearish/neutral), `impact_magnitude` (0–1 score), `event_date`. These become per-product weighted event-flag time-series.
- **FX**: `/api/market-dashboard/fx/usd-cny` endpoint exists on the dashboard; returns USD-CNY history.

## 4. Approach

**Approach A — Forecast cascade** (selected)

The dominant real-world driver of downstream chemical prices is feedstock cost-pass-through along the value chain. Approach A is the only strategy that actually models that pass-through across the horizon:

- **Training**: XGBoost trains on lagged *actual* feedstock prices, spreads, FX, and event flags — no future leakage.
- **Forecast production**: for the horizon steps, feed each downstream product the *forecasted* feedstock price read from that feedstock's own `ForecastRun` computed earlier in the same nightly batch (crude → product_c → cracked sample product → product_d, etc.).
- **Cascade error dampening**: feedstock-forecast error propagates, but is bounded by the existing honesty gate + ensemble softmax (a feedstock that's badly forecast gets low ensemble weight, and the honesty gate catches downstream products that fail to beat naive).

Rejected alternatives:
- **B (Direct multi-step, no future exogenous)**: per-horizon XGBoost using only forecast-time-known features. Cleaner (no error propagation) but loses the feedstock *trend* signal across the horizon, and multiplies model count (horizons × products).
- **C (Flat-last-known exogenous)**: hold the last observed feedstock price flat across the horizon. Simplest, no cascade, but discards the trend — weakest signal gain, and trend propagation is the whole reason feedstock matters.

### Pipeline extension

The current pipeline gains three new stages (bold) and two existing stages get extended:

```
fetch raw y
  → [NEW] preprocess (clean + report)
  → quality score
  → [NEW] build exog features (kg-driven, cascade for horizon)
  → backtest + fit  [EXTENDED: XGBoost now uses exog]
  → ensemble blend  [EXTENDED: weights blend backtest + realized]
  → [NEW] value-chain coherence (spread guardrails)
  → honesty gate
  → scenarios
  → [NEW] explain (driver attribution + NL)
  → write ForecastRun + accuracy log
  → [NEW] accuracy tracker (realized error vs prev run)
```

The nightly batch (`compute_all`) must now process targets in **topological order** (crude → product_c → cracked sample product → derivatives → downstream) so each downstream product's feedstock forecast already exists for the cascade. Topological order is derived from `KnowledgeGraph.upstream_of()`.

## 5. Components

Each component has one clear purpose, a well-defined interface, and is testable in isolation. All new modules live under `backend/app/services/forecasting/`.

### 5.1 `preprocess.py` (NEW, ~150 LOC)

**Purpose:** Clean the raw price series the models see, while keeping the raw values for reporting. Produces an auditable `cleaning_report` so operators trust the cleaning.

**Interface:**
```python
@dataclass
class CleaningReport:
    series_id: str
    n_points: int
    n_spikes_detected: int
    spike_dates: list[str]            # ISO dates
    n_level_shifts: int
    level_shift_dates: list[str]
    stale_tail_days: int | None       # None if not stale
    is_stale: bool
    winsorization_applied: bool
    notes: str

@dataclass
class PreprocessResult:
    y_clean: pd.Series                # cleaned, ready for modeling
    y_raw: pd.Series                  # original, for reporting
    report: CleaningReport

def preprocess_series(
    y: pd.Series,
    series_id: str,
    seasonal_period: int = 7,
    spike_k: float = 6.0,             # |residual| > k × MAD
    max_spike_frac: float = 0.05,     # cap at 5% of points
    stale_threshold_days: int = 14,   # tail gap > N → stale
) -> PreprocessResult:
    """Clean a price series for modeling. Never mutates the raw series."""
```

**Behavior:**
- **Stale-data guard**: detect tail gaps > `stale_threshold_days`; set `is_stale=True` so the honesty gate can warn operators. Does not abort the run.
- **Spike detection**: STL-residual based. Fit STL, compute residuals, flag points where `|residual| > spike_k × MAD(residual)` (robust to outliers, unlike z-score). Replace flagged spikes with seasonal-naive interpolation. Capped at `max_spike_frac` of points to avoid erasing real signal.
- **Level-shift detection**: marks change-points (using a rolling-mean-difference test) but does **not** remove them — regime changes are real signal, not noise.
- **Winsorization**: caps extremes (1st/99th percentile) for *training features only*; raw values kept for reporting and for the `y_clean` that the endogenous models (ARIMA/ETS/STL) see — only XGBoost sees winsorized exogenous features (handled in `feature_builder.py`, not here).
- Output: `y_clean` (spike-corrected, level-shifts preserved) + a `CleaningReport` (what was flagged/adjusted, with dates). The report is surfaced in the NL explanation (§5.6) so operators can audit the cleaning.

Reuses `quality.py`'s outlier logic conceptually but acts on the series the models see (quality.py only *scores*).

### 5.2 `features/feature_registry.py` (NEW, ~120 LOC)

**Purpose:** Auto-derive each product's exogenous feature set from the knowledge graph. Single source of truth for "what features does this product get."

**Interface:**
```python
@dataclass
class FeatureSpec:
    product_key: str
    feedstock_keys: list[str]         # ordered upstream chain (crude, product_c, ...)
    feedstock_lags: list[int]         # e.g. [1, 2, 3, 7]
    spread_pairs: list[tuple[str, str]]   # e.g. [("cracked_sample", "product_c")]
    use_fx: bool                      # USD-CNY for imported feedstocks
    use_event_flags: bool
    calendar_features: bool           # day-of-week sin/cos

def derive_feature_spec(
    product_key: str,
    graph: KnowledgeGraph,
    feedstock_lags: list[int] | None = None,
) -> FeatureSpec:
    """Derive the exogenous feature spec for a product from the KG.
    Products with no upstream (crude, product_c) return an empty feedstock list
    → endogenous-only behavior, identical to current XGBoost."""

def topological_order(
    product_keys: list[str],
    graph: KnowledgeGraph,
) -> list[str]:
    """Return product_keys in dependency order (feedstocks before derivatives).
    Uses graph.upstream_of() to build a DAG, then Kahn's algorithm."""
```

**Behavior:**
- Calls `graph.upstream_of(product)` to walk the feedstock chain. E.g. `product_d` → `[cracked_sample, product_c, product_e]`.
- Builds `spread_pairs` from adjacent feedstocks in the chain (e.g. `[("cracked_sample", "product_c"), ("product_c", "product_e")]`) — the crack spread is the canonical margin signal.
- `use_fx=True` if any feedstock is imported (crude, product_c are imported; sample product derivatives are domestic). Driven by a `is_imported` flag on `ProductNode` (already in `models.py`).
- Products with no upstream (`product_e`, `product_c`) get an empty `feedstock_keys` → `feature_builder` returns no exogenous features → XGBoost falls back to endogenous-only behavior. No regression for upstream products.
- `topological_order()` is consumed by `compute_all` to sequence the nightly batch.

### 5.3 `features/exogenous_loaders.py` (NEW, ~180 LOC)

**Purpose:** Load actual historical values for exogenous features, date-aligned to the target series.

**Interface:**
```python
class FeedstockLoader:
    def __init__(self, engine: Engine | None = None) -> None: ...
    def read_actuals(
        self,
        product_key: str,
        start_date: datetime,
        end_date: datetime,
    ) -> pd.DataFrame:
        """Returns DataFrame with columns ['ds', product_key] (price).
        Reads from md_market_price via the same EdiaMysqlDataSource config
        as the ForecastTarget for that product_key. Raises if product
        has no ForecastTarget / no data."""

class FxLoader:
    def read_usd_cny(
        self,
        start_date: datetime,
        end_date: datetime,
    ) -> pd.DataFrame:
        """Returns DataFrame with columns ['ds', 'usd_cny'].
        Reuses the existing /fx/usd-cny service logic."""

class EventFlagLoader:
    def read_flags(
        self,
        product_key: str,
        start_date: datetime,
        end_date: datetime,
        decay_window_days: int = 7,
    ) -> pd.DataFrame:
        """Returns DataFrame with columns ['ds', 'event_flag'].
        Queries ExtractedEvent store for events whose affected_commodities
        include the product (or its feedstocks). Flag value = signed
        exponentially-decayed impact: sum over events in last
        decay_window_days of direction_sign × impact_magnitude ×
        decay_factor(days_since_event). Range [-1, +1]. All-zero if
        no events (features still valid)."""
```

**Behavior:**
- `FeedstockLoader` reuses the per-product `ForecastTarget.datasource` config + `EdiaMysqlDataSource` — no new SQL, no new table mappings. The 12 products' configs already live in `seed_acme_targets.py`.
- `EventFlagLoader` queries the existing `ExtractedEvent` store (whatever table/model backs `intelligence/event_types.py`). Builds a per-product weighted flag time-series: bullish events contribute positive, bearish negative, decaying over `decay_window_days`. This captures "there was a plant outage 3 days ago with impact 0.7" as a numeric feature.
- All loaders return DataFrames date-aligned to the target series' `ds` column. Missing dates are forward-filled (last-known-value) for feedstock/FX; event flags are zero-filled for dates with no active events.

### 5.4 `features/feature_builder.py` (NEW, ~200 LOC)

**Purpose:** Assemble the exogenous feature matrix (X) aligned to the target `y`. For the forecast horizon, supply feedstock *forecast* values (the cascade).

**Interface:**
```python
@dataclass
class FeatureMatrix:
    X_train: pd.DataFrame             # features aligned to y_train (actuals only)
    X_future: pd.DataFrame            # features for the horizon (cascade for feedstock cols)
    feature_names: list[str]
    cleaning_note: str                # e.g. "2 spikes auto-corrected on 2026-07-15, 2026-07-22"

def build_features(
    target_product_key: str,
    y: pd.Series,                      # target series (cleaned)
    spec: FeatureSpec,
    feedstock_loader: FeedstockLoader,
    fx_loader: FxLoader,
    event_loader: EventFlagLoader,
    horizon: int,
    feedstock_forecasts: dict[str, list[float]] | None = None,
    # ^ keyed by feedstock product_key, value = list of horizon forecasted prices
    # None for upstream products (no cascade needed) or when cascade is degraded
) -> FeatureMatrix:
    """Build the exogenous feature matrix for training and for the horizon."""
```

**Behavior:**
- **Training features** (`X_train`): for each date in `y.index`, load feedstock actuals (lags 1/2/3/7), compute spreads (e.g. `cracked_sample_lag1 - product_c_lag1`), FX lag, event flag, calendar sin/cos. All from *actual* historical values — **no future leakage**.
- **Horizon features** (`X_future`): for each future step `t` in `[1, horizon]`:
  - Feedstock columns: read from `feedstock_forecasts[feedstock_key][t-1]` if the cascade is available. If `feedstock_forecasts is None` (upstream product, or cascade degraded), return `X_future=None` and the caller falls back to endogenous-only XGBoost.
  - Spread columns: computed from the cascade feedstock forecasts.
  - FX column: hold last-known value flat (FX has no forecast cascade in Phase 1 — acceptable; FX moves are smaller than feedstock moves).
  - Event flag column: zero (no future events known).
  - Calendar: future dates are known, so sin/cos are computed correctly.
- **Winsorization**: caps feedstock/FX feature extremes (1st/99th percentile) on `X_train` only. `X_future` uses raw cascade values (the model needs to see the actual forecasted price level).
- **Cleaning note**: propagated from `preprocess.py`'s `CleaningReport` into the NL explanation.

### 5.5 `models/xgboost_reg.py` (EXTEND, ~60 LOC added)

**Purpose:** Add optional `exog` params so XGBoost can consume exogenous features. Register as a **separate** `xgboost_exog` model so the ensemble can compare endogenous vs. exogenous XGBoost and weight accordingly.

**Interface change:**
```python
class XGBoostReg(BaseModel):
    # existing params unchanged

    def fit(self, y: pd.Series, exog: pd.DataFrame | None = None) -> "XGBoostReg":
        """If exog is None: endogenous-only (current behavior).
        If exog is provided: concatenate exog columns to the endogenous
        lag features and fit on the combined matrix."""

    def forecast(self, h: int, exog_future: pd.DataFrame | None = None) -> np.ndarray:
        """If exog_future is None: endogenous-only forecast (current behavior).
        If exog_future is provided: must have h rows; concatenate to the
        endogenous lag/rolling features built from y's tail."""
```

**New model registration** (in `models/__init__.py`):
- `xgboost_exog` — a second `XGBoostReg` instance flagged `uses_exog=True`. The ensemble sees both `xgboost_reg` (endogenous, current) and `xgboost_exog` (exogenous) as separate candidates and weights them by backtest MAPE. If exogenous features help, `xgboost_exog` gets higher weight; if they hurt (e.g. cascade error dominates), it gets lower weight and the endogenous XGBoost or naive wins. This is the self-correcting mechanism — no manual tuning.

**Behavior:**
- When `uses_exog=True` and `exog=None` (cascade degraded for this product this run), `xgboost_exog` raises `ModelNotFitError` and is skipped by the ensemble. The endogenous `xgboost_reg` still runs. Graceful degradation.
- ARIMA/ETS/STL/naive/mean_reversion stay endogenous (robust baselines). Only the two XGBoost variants consume exogenous features.

### 5.6 `reconcile.py` (NEW, ~140 LOC)

**Purpose:** Value-chain coherence — sanity checks that downstream forecasts respect their economic relationship to feedstocks. *Not* literal HTS reconciliation (which assumes additive aggregates).

**Interface:**
```python
@dataclass
class CoherenceReport:
    product_key: str
    feedstock_key: str | None
    violations: list[dict]             # each: {type, message, severity}
    direction_consistent: bool
    spread_inverted: bool
    clamped: bool                      # whether a clamp was applied

def check_coherence(
    product_key: str,
    forecast_values: list[float],      # the product's horizon forecast
    feedstock_forecast: list[float] | None,   # the feedstock's horizon forecast
    spec: FeatureSpec,
    graph: KnowledgeGraph,
    spread_inversion_threshold: float = 0.0,   # child < feedstock cost → violation
    direction_threshold_pct: float = 5.0,      # feedstock moved >X% but child didn't
) -> CoherenceReport:
    """Check value-chain coherence. Does NOT silently rewrite forecasts;
    flags violations and applies a clamp only on hard inversions."""

def apply_coherence(
    forecast_values: list[float],
    report: CoherenceReport,
    feedstock_forecast: list[float] | None,
    graph: KnowledgeGraph,
) -> tuple[list[float], CoherenceReport]:
    """Apply clamps for hard inversions (e.g. cracked_sample < product_c cost).
    Returns the (possibly adjusted) forecast + updated report."""
```

**Behavior:**
- **Spread guardrails**: if a child forecast inverts its economic relationship to the feedstock (e.g. `cracked_sample` forecasts below `product_c` cost — a negative margin that's economically unsustainable), flag a `spread_inversion` violation and clamp the child to `feedstock_forecast × (1 + min_margin)` where `min_margin` comes from the KG's `yield_rate` / historical spread percentile. Clamping is conservative — only on hard inversions, not on narrow spreads.
- **Directional consistency**: if the feedstock forecast moved > `direction_threshold_pct` over the horizon, flag downstream products that didn't move commensurately. This is a *flag for operator review*, not a clamp — the downstream product may legitimately be decoupled (e.g. long-term contract pricing).
- **No silent rewrites**: every clamp is recorded in `CoherenceReport.violations` with the original value, the clamped value, and the reason. Surfaced in the NL explanation.
- **Cascade ordering** (in `compute_all`, not here): downstream products are computed *after* their feedstocks, so `feedstock_forecast` is always available when `check_coherence` is called. If the feedstock run failed, `feedstock_forecast=None` → coherence checks are skipped (degraded mode, logged).

### 5.7 `accuracy_tracker.py` (NEW, ~160 LOC)

**Purpose:** Track realized forecast error over time, detect drift, and feed adaptive ensemble reweighting.

**Interface:**
```python
@dataclass
class RealizedAccuracy:
    product_key: str
    run_id: str
    horizon: int
    forecast_value: float
    actual_value: float | None         # None if actual not yet arrived
    realized_error: float | None       # signed (forecast - actual) / actual
    realized_mape: float | None        # absolute version
    evaluated_at: datetime | None

def evaluate_previous_run(
    db: Session,
    product_key: str,
    current_actuals: pd.Series,        # newly-arrived actuals since last run
) -> list[RealizedAccuracy]:
    """Compare the previous ForecastRun's horizon values against the
    actuals that have arrived since. Returns one RealizedAccuracy per
    horizon step that has an actual. Writes to ForecastAccuracyLog
    (extended with realized_error column)."""

def rolling_accuracy(
    db: Session,
    product_key: str,
    model_name: str | None = None,     # None = ensemble-level
    window_days: int = 30,
) -> dict:
    """Returns {'mean_mape': float, 'n_evaluations': int, 'trend': 'improving'|'degrading'|'stable'}"""

def detect_drift(
    db: Session,
    product_key: str,
    baseline_window_days: int = 90,
    recent_window_days: int = 30,
    drift_threshold_pct: float = 20.0,   # recent MAPE > baseline × (1 + threshold/100)
) -> dict:
    """Returns {'is_drifting': bool, 'baseline_mape': float,
    'recent_mape': float, 'reason': str}"""

def adaptive_weights(
    db: Session,
    product_key: str,
    backtest_weights: dict[str, float],   # current ensemble weights from backtest MAPE
    realized_weight_factor: float = 0.3,  # blend: 0.7 backtest + 0.3 realized
) -> dict[str, float]:
    """Blend backtest weights with recent realized accuracy.
    Returns renormalized weights. Falls back to backtest_weights
    if no realized accuracy is available yet (cold start)."""
```

**Behavior:**
- Each nightly run, *before* computing the new forecast, calls `evaluate_previous_run` for each product — comparing the previous run's horizon values against newly-arrived actuals. Writes `RealizedAccuracy` rows to `ForecastAccuracyLog` (extended — see §6).
- `rolling_accuracy` returns the 30-day per-model (or ensemble-level) MAPE + a trend direction.
- `detect_drift` compares recent MAPE to a 90-day baseline. If recent > baseline × 1.2, flags the product as "drifting" — surfaced in the NL explanation as a confidence caveat.
- `adaptive_weights` blends the ensemble's backtest-derived weights with recent realized accuracy. Default blend: 70% backtest, 30% realized. Cold start (no realized data yet) → 100% backtest (current behavior, no regression).

### 5.8 `explain.py` (NEW, ~180 LOC)

**Purpose:** Driver attribution + NL explanation for every `ForecastRun`. No SHAP dependency.

**Interface:**
```python
@dataclass
class DriverAttribution:
    feature_name: str
    importance: float                  # XGBoost feature_importances_ value
    contribution_delta: float | None   # how much this feature moved the forecast vs last run
                                       # None if no previous run (cold start)

@dataclass
class Explanation:
    product_key: str
    summary: str                       # 1-2 sentence NL summary
    top_drivers: list[DriverAttribution]   # top 3 by importance
    cleaning_note: str                 # from preprocess.py
    coherence_note: str                # from reconcile.py
    confidence: str                    # "high" | "medium" | "low"
    confidence_reasons: list[str]      # e.g. ["honesty gate triggered", "drift detected"]

def explain_forecast(
    product_key: str,
    forecast_values: list[float],
    previous_forecast: list[float] | None,
    xgboost_model: "XGBoostReg | None",   # the fitted exog model, if any
    feature_names: list[str],
    cleaning_report: CleaningReport,
    coherence_report: CoherenceReport,
    drift_status: dict,
    honesty_gate_triggered: bool,
) -> Explanation:
    """Generate a driver attribution + NL explanation for a forecast run."""
```

**Behavior:**
- **Driver attribution**: if `xgboost_exog` was the winning model, read its `feature_importances_` and rank features. Compute `contribution_delta` by comparing this run's forecast to the previous run's — if product_c_lag1 importance is high and the product_c forecast moved up, that's the driver. Deliberately **no SHAP** — `feature_importances_` + delta is lighter and sufficient for operator-facing explanations.
- **NL summary**: template-based, stored on `ForecastRun.explanation`. Example:
  > *"Product_D forecast +2.3% vs last run: driven by product_c +1.8% (feedstock pass-through, importance 0.42) and a bullish plant-outage event (impact 0.7, decayed over 5 days). 2 price spikes auto-corrected on 2026-07-15 and 2026-07-22. No coherence violations. Confidence: medium — honesty gate triggered (ensemble fell back to seasonal_naive)."*
- **Confidence**: `"high"` if honesty gate passed and no drift; `"medium"` if honesty gate triggered OR drift detected; `"low"` if both, OR if cascade was degraded (exog features unavailable).
- Surfaced via the existing `forecast_get` tool (which already returns `ForecastRun` rows) and the dashboard's forecast card tooltip (frontend change deferred — the explanation text is available in the API response immediately).

### 5.9 `engine.py` (EXTEND, ~120 LOC added)

**Purpose:** Wire the new stages into the pipeline. The `compute_target_anchored` method gains the new stages; `compute_all` gains topological ordering.

**Changes to `compute_target_anchored`:**
1. After fetching `y`, call `preprocess_series(y, ...)` → `PreprocessResult`. Pass `y_clean` downstream (quality, models, ensemble). Keep `y_raw` for reporting.
2. After quality scoring, call `feature_registry.derive_feature_spec(product_key, graph)` → `FeatureSpec`. If `spec.feedstock_keys` is non-empty, call `feature_builder.build_features(...)` → `FeatureMatrix`. Pass `X_train` / `X_future` to `xgboost_exog` only.
3. After ensemble blend, call `reconcile.check_coherence(...)` + `reconcile.apply_coherence(...)`. Attach `CoherenceReport` to the run.
4. After scenarios, call `explain.explain_forecast(...)` → `Explanation`. Store on `ForecastRun.explanation`.
5. Before computing the new forecast, call `accuracy_tracker.evaluate_previous_run(...)` for this product. After computing, the new run becomes the "previous" for tomorrow's evaluation.

**Changes to `compute_all`:**
1. Call `feature_registry.topological_order(product_keys, graph)` → ordered list.
2. Iterate in topological order. After each feedstock product's run completes, store its horizon forecast in an in-memory dict `feedstock_forecasts: dict[product_key, list[float]]`.
3. Pass `feedstock_forecasts` to each downstream product's `feature_builder.build_features(...)` call. Downstream products get their feedstock's *just-computed* forecast for the horizon.

**No change** to: KB-backed path, scenarios, guard (honesty gate), cache, or the existing 6 endogenous models. The pipeline is extended, not rewritten.

### 5.10 `models/forecasting.py` (EXTEND, ~15 LOC added)

**Purpose:** Add columns to support new functionality.

```python
class ForecastRun(TimestampedBase):
    # existing columns...
    explanation: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # NEW: NL explanation + driver attribution
    cleaning_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # NEW: from preprocess.py
    coherence_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # NEW: from reconcile.py
    exog_degraded: Mapped[bool] = mapped_column(Boolean, default=False)  # NEW: True if cascade unavailable

class ForecastAccuracyLog(TimestampedBase):
    # existing columns...
    realized_error: Mapped[float | None] = mapped_column(Float, nullable=True)  # NEW: signed (forecast-actual)/actual
    realized_mape: Mapped[float | None] = mapped_column(Float, nullable=True)   # NEW: absolute version
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # NEW: when actual arrived
```

### 5.11 Alembic migration `039_forecast_enhancement_phase1.py` (NEW, ~40 LOC)

- `ALTER TABLE forecast_runs ADD COLUMN explanation JSON NULL`
- `ALTER TABLE forecast_runs ADD COLUMN cleaning_report JSON NULL`
- `ALTER TABLE forecast_runs ADD COLUMN coherence_report JSON NULL`
- `ALTER TABLE forecast_runs ADD COLUMN exog_degraded BOOLEAN DEFAULT FALSE`
- `ALTER TABLE forecast_accuracy_logs ADD COLUMN realized_error FLOAT NULL`
- `ALTER TABLE forecast_accuracy_logs ADD COLUMN realized_mape FLOAT NULL`
- `ALTER TABLE forecast_accuracy_logs ADD COLUMN evaluated_at DATETIME NULL`
- Forward-only; no downgrade (additive columns only).

## 6. Data flow

### Nightly batch (2:00 AM, topological order)

1. `automation_dispatcher` ticks; spawns `AutomationExecution` for the nightly forecast task.
2. Agent loop calls `forecast_run({})`.
3. `engine.compute_all(org_id, horizons=[3,7,30], seasonal_period=7)`:
   a. Load all 12 active `ForecastTarget` rows.
   b. `topological_order(product_keys, graph)` → `[product_e, product_c, cracked_sample, cracked_market, mixed_sample, product_d, product_f, product_b, sis, styrene, blowing_agent, product_a, feedstock_a]` (feedstocks first).
   c. For each product in order:
      i. `evaluate_previous_run(db, product_key, current_actuals)` → writes `RealizedAccuracy` rows for the previous run.
      ii. Fetch `y` from `md_market_price` (or ERP fallback).
      iii. `preprocess_series(y, ...)` → `y_clean` + `CleaningReport`.
      iv. Quality score on `y_clean`.
      v. `derive_feature_spec(product_key, graph)` → `FeatureSpec`.
      vi. If `spec.feedstock_keys` non-empty: `build_features(y_clean, spec, ..., feedstock_forecasts=feedstock_forecasts)` → `FeatureMatrix`.
      vii. Fit all 7 endogenous models on `y_clean`. Fit `xgboost_exog` on `y_clean + X_train` (if `X_train` is not None).
      viii. Backtest each model (expanding window, 10 folds). For `xgboost_exog`, backtest uses only *past actual* feedstock values — **no future leakage**.
      ix. Ensemble blend: softmax over negative backtest MAPE, 0.05 floor. Blend with `adaptive_weights` (70% backtest + 30% realized, if realized data exists).
      x. `check_coherence(...)` + `apply_coherence(...)` → `CoherenceReport` (may clamp ensemble forecast).
      xi. Honesty gate: if ensemble MAPE ≥ seasonal_naive MAPE, publish naive + `below_naive_baseline=True`.
      xii. Scenarios: base/bull/bear from the published forecast.
      xiii. `explain_forecast(...)` → `Explanation`.
      xiv. Write `ForecastRun` row with `results`, `explanation`, `cleaning_report`, `coherence_report`, `exog_degraded`.
      xv. Store horizon forecast in `feedstock_forecasts[product_key]` for downstream products.
4. Returns `[ForecastRun, ...]` (12 items if all succeed).

### Daytime (dashboard poll)

1. Frontend polls `/api/market-dashboard/forecast-base-batch?day=7` every 30s (unchanged).
2. `get_forecast_base_batch_v2` reads `results["7d"]["base"]` from the latest `ForecastRun` per product (unchanged).
3. The `explanation` field is now populated and available via `forecast_get` tool for the chat agent and any future frontend panel.

## 7. Error handling

| Failure | Handling |
|---|---|
| Exogenous load failure (Acme MySQL down, no feedstock data) | `FeedstockLoader` raises. `feature_builder` returns `X_future=None`. `xgboost_exog` is skipped (not fit). Endogenous XGBoost + 6 other models run normally. `ForecastRun.exog_degraded=True`. Explanation notes "exogenous features unavailable." |
| Cascade missing (feedstock forecast not yet computed — shouldn't happen with topological order, but defensive) | `feedstock_forecasts` dict doesn't contain the key. `feature_builder` returns `X_future=None`. Same degraded path as above. |
| Event store empty | `EventFlagLoader` returns all-zero flags. Features still valid; XGBoost trains with a zero-variance column (XGBoost handles this natively — the column gets near-zero importance). |
| Cleaning over-aggression | Capped at `max_spike_frac=0.05` of points. If more than 5% would be flagged, only the top 5% by residual magnitude are corrected. Prevents erasing real signal. |
| Coherence clamp on a legitimately decoupled product | Clamps only apply on *hard inversions* (child < feedstock cost). Directional mismatches are flagged, not clamped. Operators can review flags via the explanation. |
| `xgboost_exog` overfits (backtest good, realized bad) | `adaptive_weights` (30% realized blend) down-weights `xgboost_exog` as realized accuracy degrades. Self-correcting over ~30 days. |
| Drift detected | Flagged in explanation as a confidence caveat. Does not abort the run. Operators see "Confidence: low — drift detected (recent MAPE 18% vs baseline 12%)." |
| Preprocess detects stale series | `is_stale=True` in `CleaningReport`. Explanation notes "Series is stale — no new data in 18 days." Honesty gate still runs; if it passes, the forecast publishes with the stale warning. |
| Backtest with exog features leaks future | Prevented by design: `build_features` for training uses only *past actual* feedstock values. The cascade only supplies *future* horizon at forecast-production time (step xiv). Unit test `test_no_leakage_in_backtest` asserts this. |
| Topological cycle in KG (shouldn't happen, but defensive) | `topological_order` detects cycles (Kahn's algorithm — if not all nodes are processed, a cycle exists). Raises `ValueError("Cycle in product dependency graph")`. Falls back to original serial order (current behavior) with a WARNING log. |

## 8. Testing

| Test file | What it verifies |
|---|---|
| `tests/test_preprocess.py` (NEW) | Spike detection flags STL-residual outliers; cap at 5%; replaces with seasonal-naive interpolation. Level-shift detection marks but doesn't remove. Stale-data guard sets `is_stale` correctly. Winsorization caps extremes. `y_raw` never mutated. |
| `tests/test_feature_registry.py` (NEW) | `derive_feature_spec(product_d)` returns feedstock chain `[cracked_sample, product_c, product_e]` + spread pairs. `derive_feature_spec(product_e)` returns empty feedstock list (endogenous-only). `topological_order` returns feedstocks before derivatives. Cycle detection raises. |
| `tests/test_exogenous_loaders.py` (NEW) | `FeedstockLoader.read_actuals` returns date-aligned DataFrame (mocked engine). `FxLoader` returns USD-CNY. `EventFlagLoader` returns signed decayed flags; bullish positive, bearish negative; zero when no events. |
| `tests/test_feature_builder.py` (NEW) | `build_features` produces `X_train` with correct columns (lags, spreads, FX, events, calendar). `X_future` uses cascade values for feedstock columns. `X_future=None` when `feedstock_forecasts=None` (degraded). **No-leakage test**: `X_train` for date `t` contains only feedstock actuals from `t-1` and earlier. Winsorization caps `X_train` extremes only. |
| `tests/test_xgboost_exog.py` (NEW) | `XGBoostReg.fit(y, exog)` concatenates exog to endogenous features. `forecast(h, exog_future)` requires `exog_future` to have `h` rows. `xgboost_exog` registered as separate model. When `exog=None`, `xgboost_exog` raises `ModelNotFitError` and ensemble skips it. |
| `tests/test_reconcile.py` (NEW) | `check_coherence` flags spread inversion (cracked_sample < product_c cost). `apply_coherence` clamps to `feedstock × (1 + min_margin)`. Directional mismatch flagged but not clamped. No violations when forecasts are coherent. `CoherenceReport` records original + clamped values. |
| `tests/test_accuracy_tracker.py` (NEW) | `evaluate_previous_run` compares previous horizon values to newly-arrived actuals. `rolling_accuracy` returns 30-day MAPE + trend. `detect_drift` flags when recent > baseline × 1.2. `adaptive_weights` blends 70/30 backtest/realized; cold start returns backtest weights unchanged. |
| `tests/test_explain.py` (NEW) | `explain_forecast` produces NL summary with top drivers, cleaning note, coherence note, confidence. Confidence is "low" when honesty gate triggered + drift detected. `contribution_delta` computed when previous forecast exists; None on cold start. No SHAP import. |
| `tests/test_engine_phase1_integration.py` (NEW) | End-to-end: `compute_all` on 3 synthetic products (crude → product_c → product_d) in topological order. `feedstock_forecasts` dict populated after each feedstock run. Downstream product's `xgboost_exog` uses cascade values. `ForecastRun` rows have `explanation`, `cleaning_report`, `coherence_report` populated. `exog_degraded=True` when feedstock run fails. Honesty gate still works. |
| `tests/test_no_leakage_backtest.py` (NEW) | Critical: backtest of `xgboost_exog` on date `t` uses only feedstock actuals from `t-1` and earlier. Asserts no future feedstock values appear in `X_train` for any backtest fold. |
| Existing tests | All green — no behavior change for endogenous-only path (crude, product_c) or when exog features are unavailable. Existing `test_edia_source`, `test_forecast_engine_edia_source`, `test_seed_acme_targets`, `test_forecast_dashboard_endpoint_v2`, `test_bootstrap_nightly_forecast` unchanged. |

## 9. Operational notes

- **Config flags:**
  - `FORECAST_EXOG_ENABLED` (default `true`) — gates the entire exogenous feature path. If false, engine behaves exactly as today (endogenous-only). Useful for A/B comparison or emergency rollback.
  - `FORECAST_CASCADE_ENABLED` (default `true`) — gates the cascade specifically. If false, `xgboost_exog` trains on lagged actuals but `X_future=None` (degraded mode, falls back to endogenous for horizon).
  - `FORECAST_PREPROCESS_ENABLED` (default `true`) — gates preprocessing. If false, `y_clean = y_raw` (current behavior).
  - `FORECAST_ADAPTIVE_WEIGHTS_FACTOR` (default `0.3`) — blend factor for realized accuracy in ensemble weights. `0.0` = pure backtest (current behavior).
  - `FORECAST_DRIFT_THRESHOLD_PCT` (default `20.0`) — recent MAPE must exceed baseline by this % to flag drift.
  - Existing `FORECASTING_ENABLED`, `NIGHTLY_FORECAST_ENABLED`, `NIGHTLY_FORECAST_CRON`, `BI_AGENT_ENABLED` — unchanged.
- **Backend restart:** `docker compose restart backend` (no `--reload`).
- **No frontend changes required** — explanations are available in the API response immediately. A dedicated operator-facing explanation panel is a follow-up.
- **Verify nightly output:**
  ```sql
  SELECT ft.product_key,
         fr.created_date,
         fr.results->'7d'->'base' AS base_7d,
         fr.explanation->>'summary' AS explanation,
         fr.exog_degraded,
         fr.coherence_report->>'violations' AS coherence_violations
  FROM forecast_runs fr
  JOIN forecast_targets ft ON fr.target_id = ft.id
  WHERE ft.product_key LIKE 'acme.%'
  ORDER BY ft.product_key, fr.created_date DESC;
  ```
- **Before/after comparison:** After the first nightly run with exog enabled, compare honesty-gate trigger rate and 7-day MAPE (where actuals have arrived) against the previous week's runs. Expect: lower trigger rate on downstream products (product_d, product_f, PRODUCT_B, SIS, styrene); unchanged on upstream products (crude, product_c).

## 10. Implementation order (milestones)

- **M1 — Preprocessing** (`preprocess.py` + tests): Stale guard, spike detection, level-shift marking, winsorization, `CleaningReport`. ~3 hours.
- **M2 — Exogenous features** (`features/feature_registry.py`, `features/exogenous_loaders.py`, `features/feature_builder.py`, `models/xgboost_reg.py` extension, `models/__init__.py` registration, `engine.py` wiring, `compute_all` topological ordering, tests): The core of the cascade. ~8 hours.
- **M3 — Value-chain coherence** (`reconcile.py` + tests + `engine.py` wiring): Spread guardrails, directional consistency, clamps. ~3 hours.
- **M4 — Continuous learning + interpretability** (`accuracy_tracker.py`, `explain.py`, `engine.py` wiring, tests): Realized accuracy, drift detection, adaptive weights, NL explanation. ~5 hours.
- **M5 — Integration** (migration 039, model column extensions, end-to-end test, `docker compose restart backend`, before/after comparison): Wire it all together, verify on live data. ~3 hours.

Total: ~22 hours of focused work. Decomposed into 5 PRs (one per milestone) for reviewability.

## 11. Risks

| Risk | Mitigation |
|---|---|
| Cascade error propagates (bad product_c forecast → bad product_d forecast) | Bounded by honesty gate (catches downstream failures) + ensemble softmax (down-weights `xgboost_exog` if it backtests worse) + adaptive weights (down-weights on realized error). Worst case: exog XGBoost gets ~0 weight and the forecast falls back to endogenous — no regression. |
| Backtest leakage (future feedstock values in training features) | Prevented by design (§5.4). Critical unit test `test_no_leakage_backtest` asserts no future values in any fold. |
| `xgboost_exog` overfits to exogenous features that don't generalize | Adaptive weights (30% realized blend) down-weight over ~30 days. Ensemble still has 7 endogenous baselines as backup. |
| Event store schema unknown / not populated | `EventFlagLoader` returns all-zero flags when store is empty — features still valid, XGBoost handles zero-variance columns. No hard dependency on events existing. |
| KG cycle breaks topological ordering | `topological_order` detects cycles (Kahn's algorithm) and falls back to serial order with a WARNING. No crash. |
| Preprocess erases real signal (over-aggressive spike correction) | Capped at 5% of points. Level-shifts never removed. `y_raw` always preserved for reporting. Operators can audit via `cleaning_report`. |
| Coherence clamp produces economically wrong clamp value | `min_margin` derived from KG `yield_rate` + historical spread percentile. Conservative — only clamps on hard inversions (negative margin), not narrow spreads. All clamps logged with original + clamped value. |
| Migration 039 fails on existing deployments | Additive columns only (all nullable / have defaults). No data backfill needed. Forward-only, no downgrade. |
| Performance: 12 products × exog feature building adds latency | Each product's exog load is ~1 SQL query (feedstock actuals) + 1 query (events) + 1 query (FX). ~3 queries × 12 products = ~36 extra queries total. Negligible vs. the ~30s/model fitting time. Topological ordering adds no latency (in-memory dict). |
| `ForecastAccuracyLog` extension breaks existing accuracy tool | New columns are nullable. Existing rows have NULL `realized_error` / `realized_mape` / `evaluated_at`. Existing `forecast_accuracy` tool reads `mape` (unchanged column). No regression. |

## 12. Success criteria

- **Honesty-gate trigger rate drops below ~50% of products** (from ~most today) — measured over the first 7 nightly runs after deployment.
- **Per-product 7-day MAPE improves vs. current endogenous ensemble** where exogenous signal exists (downstream products: product_d, product_f, PRODUCT_B, SIS, styrene). Upstream products (crude, product_c) unchanged — they have no feedstock signal.
- **Every `ForecastRun` carries an `explanation` + `cleaning_report` + `coherence_report`** — no NULLs in the new columns for any successfully-completed run.
- **No regression**: products without feedstock signal (crude, product_c) or when exog is degraded (Acme MySQL down) produce identical forecasts to the current endogenous-only pipeline. Verified by A/B: run with `FORECAST_EXOG_ENABLED=false` and compare.
- **No backtest leakage**: `test_no_leakage_backtest` passes. Manual spot-check: for a downstream product, `X_train` for the last backtest fold contains only feedstock actuals from before that fold's cutoff.
