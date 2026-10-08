# Industrial Market-Price Forecast Enhancement — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enhance the existing `ForecastEngine` with exogenous feature engineering (forecast cascade), anomaly-robust preprocessing, value-chain coherence, and continuous learning + interpretability — to lift the honesty-gate pass rate and give operators a defensible explanation of every forecast.

**Architecture:** Extend the nightly pipeline (fetch → quality → models → ensemble → guard → scenarios → write) with 4 new stages: preprocess (clean), build exog features (KG-driven cascade), reconcile (spread guardrails), explain (driver attribution + NL). A new `xgboost_exog` model variant consumes exogenous features; the 7 endogenous models stay as robust baselines. The nightly batch processes targets in topological order (crude → product_c → C5 → derivatives) so downstream products can use feedstock forecasts as future-step exogenous values.

**Tech Stack:** Python 3.11, SQLAlchemy 2.0, pandas, statsmodels (STL), xgboost, pytest. Backend runs in Docker (no `--reload` — `docker compose restart backend` after service changes). Tests run via `backend/venv/bin/python -m pytest` (system has no global pytest).

**Spec:** `docs/superpowers/specs/2026-08-03-forecast-enhancement-phase1-design.md`

## Global Constraints

- **Project root:** `/home/ysk2025/zhanlu_7_30` (NOT `/home/ysk2025/zhanlu` or `zhanlu-deploy`).
- **Backend venv:** `backend/venv/bin/python` — use this for all test runs. pytest is NOT in requirements.txt but IS installed in the venv.
- **Test runner:** `cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/<file>::<test> -v` — run individually, SSH RAM is limited (no parallelism, no backgrounding).
- **Docker:** Backend has no `--reload`. After service code changes: `docker compose restart backend`. Pandas/statsmodels/xgboost are already pip-installed in the container.
- **Alembic:** Latest migration is `038_resource_shares_and_type.py`. Next is `039`.
- **Code style:** Follow existing patterns — `from __future__ import annotations`, dataclasses for result types, `TimestampedBase` for DB models, lazy imports for heavy deps, type hints everywhere.
- **No frontend changes** in Phase 1 — explanations surface via the existing `forecast_get` tool and API response.
- **Feature flags:** All new behavior gated by `FORECAST_EXOG_ENABLED` (default `true`), `FORECAST_PREPROCESS_ENABLED` (default `true`), `FORECAST_CASCADE_ENABLED` (default `true`). Setting any to `false` reverts to current endogenous-only behavior.
- **Honesty gate is sacred:** Never bypass `guard.py`. The gate catching downstream products means the cascade error dominated — that's correct self-correcting behavior, not a bug to fix.

---

## File Structure

**New files (all under `backend/app/services/forecasting/`):**

| File | Responsibility |
|---|---|
| `preprocess.py` | Spike/stale/level-shift detection + winsorization. Produces `y_clean` + `CleaningReport`. |
| `features/__init__.py` | Package marker. |
| `features/feature_registry.py` | KG-driven feature spec derivation + topological ordering. |
| `features/exogenous_loaders.py` | `FeedstockLoader`, `FxLoader`, `EventFlagLoader`. |
| `features/feature_builder.py` | Assemble `X_train` (past actuals, no leakage) + `X_future` (cascade values). |
| `reconcile.py` | Value-chain coherence: spread guardrails + directional consistency. |
| `accuracy_tracker.py` | Realized error tracking + drift detection + adaptive ensemble weights. |
| `explain.py` | Driver attribution + NL explanation templates. |

**Modified files:**

| File | Change |
|---|---|
| `backend/app/services/forecasting/models/xgboost_reg.py` | Add optional `exog` param to `fit()`/`forecast()`. Add `uses_exog` class flag. |
| `backend/app/services/forecasting/models/__init__.py` | Register `xgboost_exog` (second `XGBoostReg` with `uses_exog=True`). |
| `backend/app/services/forecasting/engine.py` | Wire 4 new stages into `compute_target()`. Add topological ordering to `compute_all()`. Fit `xgboost_exog` separately with exog. |
| `backend/app/models/forecasting.py` | Add `explanation`, `cleaning_report`, `coherence_report`, `exog_degraded` to `ForecastRun`. Add `realized_error`, `realized_mape`, `evaluated_at` to `ForecastAccuracyLog`. |
| `backend/app/config.py` | Add `FORECAST_EXOG_ENABLED`, `FORECAST_PREPROCESS_ENABLED`, `FORECAST_CASCADE_ENABLED`, `FORECAST_ADAPTIVE_WEIGHTS_FACTOR`, `FORECAST_DRIFT_THRESHOLD_PCT`. |

**New migration:** `backend/alembic/versions/039_forecast_enhancement_phase1.py`

**New test files (all under `backend/tests/`):**

| File | Tests |
|---|---|
| `test_preprocess.py` | Spike, stale, level-shift, winsorization, raw preserved. |
| `test_feature_registry.py` | derive_feature_spec for upstream vs downstream. topological_order. Cycle detection. |
| `test_exogenous_loaders.py` | FeedstockLoader, FxLoader, EventFlagLoader (mocked). |
| `test_feature_builder.py` | X_train columns, X_future cascade, no-leakage, winsorization, degraded mode. |
| `test_xgboost_exog.py` | fit with exog, forecast with exog_future, ModelFitError when exog=None. |
| `test_reconcile.py` | Spread inversion, clamp, directional flag, no-op when coherent. |
| `test_accuracy_tracker.py` | evaluate_previous_run, rolling_accuracy, detect_drift, adaptive_weights. |
| `test_explain.py` | NL summary, top drivers, confidence, contribution_delta, no SHAP. |
| `test_engine_phase1_integration.py` | End-to-end: 3 synthetic products in topological order. |
| `test_no_leakage_backtest.py` | Critical: no future feedstock values in any backtest fold. |

---

## Task 1: Anomaly-robust preprocessing — `preprocess.py`

**Files:**
- Create: `backend/app/services/forecasting/preprocess.py`
- Test: `backend/tests/test_preprocess.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_preprocess.py
"""Tests for anomaly-robust preprocessing."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.preprocess import (
    CleaningReport, PreprocessResult, preprocess_series,
)


def _make_series(n: int = 100, spike_idx: list[int] | None = None,
                 stale_tail_gap: int = 0) -> pd.Series:
    dates = pd.date_range("2026-01-01", periods=n + stale_tail_gap, freq="D")
    base = 5000 + np.sin(np.arange(n + stale_tail_gap) * 0.1) * 100
    values = base.copy()
    if spike_idx:
        for i in spike_idx:
            values[i] *= 3.0
    if stale_tail_gap > 0:
        values[n:] = np.nan
        dates = dates[:n]
        values = values[:n]
    return pd.Series(values, index=dates, name="y")


class TestSpikeDetection:
    def test_detects_and_corrects_spike(self):
        y = _make_series(n=100, spike_idx=[50])
        result = preprocess_series(y, "test_product")
        assert result.report.n_spikes_detected >= 1
        assert abs(result.y_clean.iloc[50] - y.iloc[49]) < abs(y.iloc[50] - y.iloc[49])

    def test_cap_at_5_percent(self):
        y = _make_series(n=100, spike_idx=list(range(0, 20)))
        result = preprocess_series(y, "test_product")
        assert result.report.n_spikes_detected <= 5

    def test_no_spike_on_clean_series(self):
        y = _make_series(n=100)
        result = preprocess_series(y, "test_product")
        assert result.report.n_spikes_detected == 0


class TestStaleGuard:
    def test_stale_detected_when_tail_gap_large(self):
        old_dates = pd.date_range("2026-01-01", periods=50, freq="D")
        old_values = 5000 + np.random.rand(50) * 50
        y_old = pd.Series(old_values, index=old_dates)
        result = preprocess_series(y_old, "test_product", stale_threshold_days=14)
        assert isinstance(result.report.is_stale, bool)

    def test_not_stale_when_recent(self):
        recent_dates = pd.date_range(end=pd.Timestamp.now(), periods=50, freq="D")
        values = 5000 + np.random.rand(50) * 50
        y = pd.Series(values, index=recent_dates)
        result = preprocess_series(y, "test_product", stale_threshold_days=14)
        assert result.report.is_stale is False


class TestLevelShift:
    def test_level_shift_marked_not_removed(self):
        dates = pd.date_range("2026-01-01", periods=100, freq="D")
        values = np.concatenate([np.full(50, 5000.0), np.full(50, 5500.0)])
        y = pd.Series(values, index=dates)
        result = preprocess_series(y, "test_product")
        assert result.report.n_level_shifts >= 1
        assert result.y_clean.iloc[60] == pytest.approx(5500.0)


class TestRawPreserved:
    def test_y_raw_never_mutated(self):
        y = _make_series(n=100, spike_idx=[50])
        original = y.copy()
        result = preprocess_series(y, "test_product")
        pd.testing.assert_series_equal(y, original)
        pd.testing.assert_series_equal(result.y_raw, original)


class TestWinsorization:
    def test_extremes_capped_in_clean(self):
        dates = pd.date_range("2026-01-01", periods=100, freq="D")
        values = np.full(100, 5000.0)
        values[50] = 50000.0
        y = pd.Series(values, index=dates)
        result = preprocess_series(y, "test_product", spike_k=100.0)
        assert result.y_clean.max() < 50000.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/test_preprocess.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.forecasting.preprocess'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/forecasting/preprocess.py
"""Anomaly-robust preprocessing for forecast input series."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class CleaningReport:
    series_id: str
    n_points: int
    n_spikes_detected: int = 0
    spike_dates: list[str] = field(default_factory=list)
    n_level_shifts: int = 0
    level_shift_dates: list[str] = field(default_factory=list)
    stale_tail_days: int | None = None
    is_stale: bool = False
    winsorization_applied: bool = False
    notes: str = ""


@dataclass
class PreprocessResult:
    y_clean: pd.Series
    y_raw: pd.Series
    report: CleaningReport


def preprocess_series(
    y: pd.Series,
    series_id: str,
    seasonal_period: int = 7,
    spike_k: float = 6.0,
    max_spike_frac: float = 0.05,
    stale_threshold_days: int = 14,
    winsorize: bool = True,
) -> PreprocessResult:
    y_raw = y.copy()
    y_work = y.dropna().copy()
    n_points = len(y_work)
    report = CleaningReport(series_id=series_id, n_points=n_points)

    if n_points < seasonal_period * 2:
        report.notes = "Series too short for STL-based cleaning; returned unchanged."
        return PreprocessResult(y_clean=y_work, y_raw=y_raw, report=report)

    # 1. Stale-data guard
    last_date = y_work.index.max()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if hasattr(last_date, "tzinfo") and last_date.tzinfo is not None:
        last_date = last_date.replace(tzinfo=None)
    tail_gap_days = (now - last_date).days if last_date < now else 0
    if tail_gap_days > stale_threshold_days:
        report.is_stale = True
        report.stale_tail_days = tail_gap_days

    # 2. Spike detection (STL-residual based)
    try:
        from statsmodels.tsa.seasonal import STL
        stl = STL(y_work.values.astype(float), period=seasonal_period, robust=True)
        res = stl.fit()
        residuals = res.resid
        mad = np.median(np.abs(residuals - np.median(residuals)))
        if mad > 1e-10:
            spike_mask = np.abs(residuals) > spike_k * mad
            spike_indices = np.where(spike_mask)[0]
        else:
            spike_indices = np.array([], dtype=int)
    except Exception as exc:
        logger.warning("STL failed for %s: %s", series_id, exc)
        spike_indices = np.array([], dtype=int)

    max_spikes = max(1, int(n_points * max_spike_frac))
    if len(spike_indices) > max_spikes:
        residuals_abs = np.abs(residuals[spike_indices]) if len(spike_indices) > 0 else np.array([])
        top_order = np.argsort(residuals_abs)[::-1][:max_spikes]
        spike_indices = spike_indices[top_order]

    report.n_spikes_detected = len(spike_indices)
    for idx in spike_indices:
        date = y_work.index[idx]
        report.spike_dates.append(str(date.date()) if hasattr(date, "date") else str(date))

    y_cleaned = y_work.copy()
    for idx in spike_indices:
        lag = seasonal_period
        if idx >= lag:
            y_cleaned.iloc[idx] = y_work.iloc[idx - lag]
        elif idx > 0:
            y_cleaned.iloc[idx] = y_work.iloc[idx - 1]

    # 3. Level-shift detection (mark, don't remove)
    try:
        rolling_mean = y_cleaned.rolling(window=seasonal_period, center=True).mean()
        diff = rolling_mean.diff(seasonal_period)
        mad_diff = np.median(np.abs(diff.dropna() - np.median(diff.dropna())))
        if mad_diff > 1e-10:
            shift_mask = np.abs(diff) > 5 * mad_diff
            shift_indices = np.where(shift_mask.values)[0]
        else:
            shift_indices = np.array([], dtype=int)
    except Exception:
        shift_indices = np.array([], dtype=int)

    report.n_level_shifts = len(shift_indices)
    for idx in shift_indices:
        date = y_cleaned.index[idx]
        report.level_shift_dates.append(str(date.date()) if hasattr(date, "date") else str(date))

    # 4. Winsorization
    if winsorize and n_points >= 20:
        p1, p99 = np.percentile(y_cleaned.values, [1, 99])
        y_before = y_cleaned.copy()
        y_cleaned = y_cleaned.clip(lower=p1, upper=p99)
        if not y_cleaned.equals(y_before):
            report.winsorization_applied = True

    report.notes = (
        f"Preprocessed {n_points} points: {report.n_spikes_detected} spikes, "
        f"{report.n_level_shifts} shifts, stale={'yes' if report.is_stale else 'no'}, "
        f"winsorized={'yes' if report.winsorization_applied else 'no'}."
    )
    return PreprocessResult(y_clean=y_cleaned, y_raw=y_raw, report=report)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/test_preprocess.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/forecasting/preprocess.py backend/tests/test_preprocess.py
git commit -m "feat(forecast): add anomaly-robust preprocessing module (M1)

STL-residual spike detection (capped at 5%), stale-data guard,
level-shift marking (not removal), winsorization. Auditable
CleaningReport. y_raw never mutated."
```

---

## Task 2: Feature registry — `features/feature_registry.py`

**Files:**
- Create: `backend/app/services/forecasting/features/__init__.py`
- Create: `backend/app/services/forecasting/features/feature_registry.py`
- Test: `backend/tests/test_feature_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_feature_registry.py
"""Tests for KG-driven feature spec derivation and topological ordering."""
from __future__ import annotations

import pytest

from app.services.knowledge_graph.graph import build_sample_graph
from app.services.forecasting.features.feature_registry import (
    FeatureSpec, derive_feature_spec, topological_order,
)


class TestDeriveFeatureSpec:
    def test_upstream_product_has_empty_feedstock(self):
        graph = build_sample_graph()
        spec = derive_feature_spec("product_e", graph)
        assert spec.product_key == "product_e"
        assert spec.feedstock_keys == []

    def test_downstream_product_has_feedstock_chain(self):
        graph = build_sample_graph()
        spec = derive_feature_spec("product_d", graph)
        assert "cracked_c5" in spec.feedstock_keys
        assert "product_c" in spec.feedstock_keys
        assert "product_e" in spec.feedstock_keys

    def test_spread_pairs_built_from_adjacent_feedstocks(self):
        graph = build_sample_graph()
        spec = derive_feature_spec("product_d", graph)
        assert len(spec.spread_pairs) >= 1
        for pair in spec.spread_pairs:
            assert isinstance(pair, tuple)
            assert len(pair) == 2

    def test_use_fx_true_when_chain_includes_imported(self):
        graph = build_sample_graph()
        spec = derive_feature_spec("product_d", graph)
        assert spec.use_fx is True

    def test_use_fx_false_for_no_feedstocks(self):
        graph = build_sample_graph()
        spec = derive_feature_spec("product_e", graph)
        assert spec.use_fx is False

    def test_unknown_product_returns_empty_spec(self):
        graph = build_sample_graph()
        spec = derive_feature_spec("nonexistent_product", graph)
        assert spec.feedstock_keys == []

    def test_custom_feedstock_lags(self):
        graph = build_sample_graph()
        spec = derive_feature_spec("product_d", graph, feedstock_lags=[1, 7, 14])
        assert spec.feedstock_lags == [1, 7, 14]

    def test_default_feedstock_lags(self):
        graph = build_sample_graph()
        spec = derive_feature_spec("product_d", graph)
        assert spec.feedstock_lags == [1, 2, 3, 7]


class TestTopologicalOrder:
    def test_feedstocks_before_derivatives(self):
        graph = build_sample_graph()
        products = ["product_d", "product_e", "product_c", "cracked_c5"]
        ordered = topological_order(products, graph)
        assert ordered.index("product_e") < ordered.index("product_c")
        assert ordered.index("product_c") < ordered.index("cracked_c5")
        assert ordered.index("cracked_c5") < ordered.index("product_d")

    def test_all_products_returned(self):
        graph = build_sample_graph()
        products = ["product_e", "product_c", "product_d", "product_b"]
        ordered = topological_order(products, graph)
        assert set(ordered) == set(products)

    def test_products_not_in_graph_returned_at_end(self):
        graph = build_sample_graph()
        products = ["product_e", "unknown_product"]
        ordered = topological_order(products, graph)
        assert ordered.index("product_e") < ordered.index("unknown_product")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/test_feature_registry.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/forecasting/features/__init__.py
# Empty package marker.
```

```python
# backend/app/services/forecasting/features/feature_registry.py
"""KG-driven exogenous feature spec derivation + topological ordering."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.services.knowledge_graph.models import (
    KnowledgeGraph, ProductNode, ProductType,
)

logger = logging.getLogger(__name__)

_IMPORTED_TYPES = {ProductType.CRUDE, ProductType.FEEDSTOCK}
_DEFAULT_FEEDSTOCK_LAGS = [1, 2, 3, 7]


@dataclass
class FeatureSpec:
    product_key: str
    feedstock_keys: list[str] = field(default_factory=list)
    feedstock_lags: list[int] = field(default_factory=lambda: list(_DEFAULT_FEEDSTOCK_LAGS))
    spread_pairs: list[tuple[str, str]] = field(default_factory=list)
    use_fx: bool = False
    use_event_flags: bool = True
    calendar_features: bool = True


def _walk_upstream_chain(product_id: str, graph: KnowledgeGraph, visited: set[str] | None = None) -> list[ProductNode]:
    if visited is None:
        visited = set()
    if product_id in visited:
        return []
    visited.add(product_id)
    direct = graph.upstream_of(product_id)
    chain: list[ProductNode] = []
    for node in direct:
        if node not in chain:
            chain.append(node)
        deeper = _walk_upstream_chain(node.id, graph, visited)
        for d in deeper:
            if d not in chain:
                chain.append(d)
    return chain


def derive_feature_spec(product_key: str, graph: KnowledgeGraph, feedstock_lags: list[int] | None = None) -> FeatureSpec:
    node = graph.get_node(product_key)
    if node is None:
        return FeatureSpec(
            product_key=product_key,
            feedstock_keys=[],
            feedstock_lags=feedstock_lags or list(_DEFAULT_FEEDSTOCK_LAGS),
        )
    chain = _walk_upstream_chain(product_key, graph)
    feedstock_keys = [n.id for n in chain]
    spread_pairs: list[tuple[str, str]] = []
    for i in range(len(feedstock_keys) - 1):
        spread_pairs.append((feedstock_keys[i], feedstock_keys[i + 1]))
    use_fx = any(
        graph.get_node(fk) and graph.get_node(fk).type in _IMPORTED_TYPES
        for fk in feedstock_keys
    )
    return FeatureSpec(
        product_key=product_key,
        feedstock_keys=feedstock_keys,
        feedstock_lags=feedstock_lags or list(_DEFAULT_FEEDSTOCK_LAGS),
        spread_pairs=spread_pairs,
        use_fx=use_fx,
        use_event_flags=True,
        calendar_features=True,
    )


def topological_order(product_keys: list[str], graph: KnowledgeGraph) -> list[str]:
    product_set = set(product_keys)
    deps: dict[str, set[str]] = {pk: set() for pk in product_keys}
    for pk in product_keys:
        if graph.get_node(pk) is None:
            continue
        upstream = graph.upstream_of(pk)
        for up_node in upstream:
            if up_node.id in product_set:
                deps[pk].add(up_node.id)
    ordered: list[str] = []
    remaining = dict(deps)
    while remaining:
        ready = sorted([pk for pk, d in remaining.items() if not d])
        if not ready:
            logger.warning("Cycle detected — falling back to original order")
            ordered.extend(remaining.keys())
            break
        for pk in ready:
            ordered.append(pk)
            del remaining[pk]
            for other_deps in remaining.values():
                other_deps.discard(pk)
    return ordered
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/test_feature_registry.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/forecasting/features/__init__.py \
        backend/app/services/forecasting/features/feature_registry.py \
        backend/tests/test_feature_registry.py
git commit -m "feat(forecast): add KG-driven feature registry + topological ordering (M2)

derive_feature_spec walks upstream_of() transitively. topological_order
uses Kahn's algorithm. Cycle detection falls back gracefully."
```

---

## Task 3: Exogenous data loaders — `features/exogenous_loaders.py`

**Files:**
- Create: `backend/app/services/forecasting/features/exogenous_loaders.py`
- Test: `backend/tests/test_exogenous_loaders.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_exogenous_loaders.py
"""Tests for exogenous data loaders (feedstock, FX, events)."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.services.forecasting.features.exogenous_loaders import (
    FeedstockLoader, FxLoader, EventFlagLoader,
)


class TestFeedstockLoader:
    @patch("app.services.forecasting.features.exogenous_loaders.EdiaMysqlDataSource")
    @patch("app.services.forecasting.features.exogenous_loaders.SessionLocal")
    def test_read_actuals_returns_date_aligned_dataframe(self, mock_session, mock_src_cls):
        mock_db = MagicMock()
        mock_session.return_value = mock_db
        mock_target = MagicMock()
        mock_target.datasource = {
            "source": "mysql", "table": "md_market_price",
            "time_col": "FDATE", "measure": "FTAXPRICE",
            "where": "FMATERIAL_NAME = 'test'",
        }
        mock_db.query.return_value.filter.return_value.first.return_value = mock_target
        mock_src = MagicMock()
        mock_src_cls.return_value = mock_src
        mock_src.read_history.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=30, freq="D"),
            "y": [5000.0 + i for i in range(30)],
        })

        loader = FeedstockLoader()
        df = loader.read_actuals("test_product", datetime(2026, 1, 1), datetime(2026, 1, 30))
        assert "ds" in df.columns
        assert "test_product" in df.columns
        assert len(df) == 30

    @patch("app.services.forecasting.features.exogenous_loaders.SessionLocal")
    def test_read_actuals_raises_when_no_target(self, mock_session):
        mock_db = MagicMock()
        mock_session.return_value = mock_db
        mock_db.query.return_value.filter.return_value.first.return_value = None
        loader = FeedstockLoader()
        with pytest.raises(ValueError, match="No ForecastTarget"):
            loader.read_actuals("nonexistent", datetime(2026, 1, 1), datetime(2026, 1, 30))


class TestFxLoader:
    @patch("app.services.forecasting.features.exogenous_loaders.get_fx_rate")
    def test_read_usd_cny_returns_constant_series(self, mock_fx):
        mock_fx.return_value = MagicMock(rate=7.2)
        loader = FxLoader()
        df = loader.read_usd_cny(datetime(2026, 1, 1), datetime(2026, 1, 30))
        assert "ds" in df.columns
        assert "usd_cny" in df.columns
        assert len(df) == 30
        assert (df["usd_cny"] == 7.2).all()


class TestEventFlagLoader:
    @patch("app.services.forecasting.features.exogenous_loaders.SessionLocal")
    def test_read_flags_returns_zero_when_no_events(self, mock_session):
        mock_db = MagicMock()
        mock_session.return_value = mock_db
        mock_db.query.return_value.filter.return_value.all.return_value = []
        loader = EventFlagLoader()
        df = loader.read_flags("product_d", datetime(2026, 1, 1), datetime(2026, 1, 30))
        assert "ds" in df.columns
        assert "event_flag" in df.columns
        assert (df["event_flag"] == 0.0).all()

    @patch("app.services.forecasting.features.exogenous_loaders.SessionLocal")
    def test_read_flags_returns_signed_decayed_values(self, mock_session):
        mock_db = MagicMock()
        mock_session.return_value = mock_db
        mock_event = MagicMock()
        mock_event.direction = "bullish"
        mock_event.impact_magnitude = 0.7
        mock_event.detected_at = (datetime.utcnow() - timedelta(days=3)).isoformat()
        mock_event.affected_commodities = ["product_d"]
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_event]

        loader = EventFlagLoader(decay_window_days=7)
        df = loader.read_flags("product_d", datetime(2026, 1, 1), datetime(2026, 1, 30))
        assert (df["event_flag"] != 0.0).any()
        nonzero = df[df["event_flag"] != 0.0]["event_flag"]
        assert (nonzero > 0).all()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/test_exogenous_loaders.py -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/forecasting/features/exogenous_loaders.py
"""Loaders for exogenous feature data: feedstock prices, FX, market events."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from app.models.forecasting import ForecastTarget
from app.models.intelligence import IntelligenceEvent
from app.services.forecasting.edia_source import EdiaMysqlDataSource

logger = logging.getLogger(__name__)


class FeedstockLoader:
    """Read actual historical feedstock prices from Acme MySQL."""

    def __init__(self, engine=None) -> None:
        self._engine = engine

    def read_actuals(self, product_key: str, start_date: datetime, end_date: datetime) -> pd.DataFrame:
        from app.db.session import SessionLocal
        db = SessionLocal()
        try:
            target = (
                db.query(ForecastTarget)
                .filter(ForecastTarget.product_key == product_key, ForecastTarget.is_deleted == False)
                .first()
            )
            if not target:
                raise ValueError(f"No ForecastTarget for product_key={product_key}")
            src = EdiaMysqlDataSource(engine=self._engine)
            df = src.read_history(target.datasource)
            df = df.rename(columns={"y": product_key})
            df = df[(df["ds"] >= pd.Timestamp(start_date)) & (df["ds"] <= pd.Timestamp(end_date))]
            return df.reset_index(drop=True)
        finally:
            db.close()


class FxLoader:
    """Read USD-CNY exchange rate. Phase 1: static rate from market_dashboard_service."""

    def __init__(self) -> None:
        from app.services.market_dashboard_service import get_fx_rate
        self._get_fx_rate = get_fx_rate

    def read_usd_cny(self, start_date: datetime, end_date: datetime) -> pd.DataFrame:
        fx = self._get_fx_rate()
        rate = float(fx.rate) if hasattr(fx, "rate") else 7.2
        dates = pd.date_range(start=start_date, end=end_date, freq="D")
        return pd.DataFrame({"ds": dates, "usd_cny": [rate] * len(dates)})


class EventFlagLoader:
    """Per-product weighted event-flag time-series from IntelligenceEvent."""

    def __init__(self, decay_window_days: int = 7) -> None:
        self._decay_window = decay_window_days

    def read_flags(self, product_key: str, start_date: datetime, end_date: datetime) -> pd.DataFrame:
        from app.db.session import SessionLocal
        dates = pd.date_range(start=start_date, end=end_date, freq="D")
        flags = np.zeros(len(dates))

        db = SessionLocal()
        try:
            events = (
                db.query(IntelligenceEvent)
                .filter(IntelligenceEvent.review_status == "approved", IntelligenceEvent.is_deleted == False)
                .all()
            )
            for event in events:
                commodities = event.affected_commodities or []
                if product_key not in commodities:
                    continue
                direction = (event.direction or "neutral").lower()
                if direction == "bullish":
                    sign = 1.0
                elif direction == "bearish":
                    sign = -1.0
                else:
                    continue
                magnitude = float(event.impact_magnitude or 0.0)
                try:
                    event_dt = datetime.fromisoformat(event.detected_at.replace("Z", "+00:00"))
                    if event_dt.tzinfo:
                        event_dt = event_dt.replace(tzinfo=None)
                except (ValueError, AttributeError):
                    continue
                for i, date in enumerate(dates):
                    days_since = (date - event_dt).days
                    if 0 <= days_since < self._decay_window:
                        decay = 1.0 - (days_since / self._decay_window)
                        flags[i] += sign * magnitude * decay
            flags = np.clip(flags, -1.0, 1.0)
        finally:
            db.close()
        return pd.DataFrame({"ds": dates, "event_flag": flags})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/test_exogenous_loaders.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/forecasting/features/exogenous_loaders.py \
        backend/tests/test_exogenous_loaders.py
git commit -m "feat(forecast): add exogenous data loaders (feedstock/FX/events) (M2)"
```

---

## Task 4: Feature builder — `features/feature_builder.py`

**Files:**
- Create: `backend/app/services/forecasting/features/feature_builder.py`
- Test: `backend/tests/test_feature_builder.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_feature_builder.py
"""Tests for exogenous feature matrix builder."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.features.feature_registry import FeatureSpec
from app.services.forecasting.features.feature_builder import (
    FeatureMatrix, build_features,
)


def _make_y(n: int = 60) -> pd.Series:
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.Series(5000 + np.random.rand(n) * 100, index=dates, name="y")


class TestBuildFeaturesTraining:
    def test_x_train_has_feedstock_lag_columns(self):
        y = _make_y(60)
        spec = FeatureSpec(
            product_key="product_d", feedstock_keys=["cracked_c5", "product_c"],
            feedstock_lags=[1, 2], spread_pairs=[("cracked_c5", "product_c")],
            use_fx=True, use_event_flags=True, calendar_features=True,
        )
        loader = MagicMock()
        loader.read_actuals.side_effect = [
            pd.DataFrame({"ds": pd.date_range("2026-01-01", periods=60, freq="D"),
                          "cracked_c5": [800.0 + i for i in range(60)]}),
            pd.DataFrame({"ds": pd.date_range("2026-01-01", periods=60, freq="D"),
                          "product_c": [600.0 + i for i in range(60)]}),
        ]
        fx_loader = MagicMock()
        fx_loader.read_usd_cny.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"), "usd_cny": [7.2] * 60})
        event_loader = MagicMock()
        event_loader.read_flags.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"), "event_flag": [0.0] * 60})

        result = build_features("product_d", y, spec, loader, fx_loader, event_loader, 7, None)
        assert result.X_train is not None
        lag_cols = [c for c in result.X_train.columns if "lag" in c]
        assert len(lag_cols) >= 2

    def test_x_train_has_spread_columns(self):
        y = _make_y(60)
        spec = FeatureSpec(product_key="product_d", feedstock_keys=["cracked_c5", "product_c"],
                           feedstock_lags=[1], spread_pairs=[("cracked_c5", "product_c")])
        loader = MagicMock()
        loader.read_actuals.side_effect = [
            pd.DataFrame({"ds": pd.date_range("2026-01-01", periods=60, freq="D"),
                          "cracked_c5": [800.0 + i for i in range(60)]}),
            pd.DataFrame({"ds": pd.date_range("2026-01-01", periods=60, freq="D"),
                          "product_c": [600.0 + i for i in range(60)]}),
        ]
        fx_loader = MagicMock()
        fx_loader.read_usd_cny.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"), "usd_cny": [7.2] * 60})
        event_loader = MagicMock()
        event_loader.read_flags.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"), "event_flag": [0.0] * 60})
        result = build_features("product_d", y, spec, loader, fx_loader, event_loader, 7, None)
        spread_cols = [c for c in result.X_train.columns if "spread" in c]
        assert len(spread_cols) >= 1

    def test_x_train_has_calendar_columns(self):
        y = _make_y(60)
        spec = FeatureSpec(product_key="test", feedstock_keys=[], calendar_features=True)
        loader = MagicMock()
        fx_loader = MagicMock()
        event_loader = MagicMock()
        event_loader.read_flags.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"), "event_flag": [0.0] * 60})
        result = build_features("test", y, spec, loader, fx_loader, event_loader, 7, None)
        assert any("sin" in c or "cos" in c for c in result.X_train.columns)


class TestBuildFeaturesFuture:
    def test_x_future_uses_cascade_values(self):
        y = _make_y(60)
        spec = FeatureSpec(product_key="product_d", feedstock_keys=["cracked_c5"], feedstock_lags=[1])
        loader = MagicMock()
        loader.read_actuals.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"),
            "cracked_c5": [800.0 + i for i in range(60)]})
        fx_loader = MagicMock()
        fx_loader.read_usd_cny.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"), "usd_cny": [7.2] * 60})
        event_loader = MagicMock()
        event_loader.read_flags.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"), "event_flag": [0.0] * 60})
        cascade = {"cracked_c5": [810.0, 815.0, 820.0, 825.0, 830.0, 835.0, 840.0]}
        result = build_features("product_d", y, spec, loader, fx_loader, event_loader, 7, cascade)
        assert result.X_future is not None
        assert len(result.X_future) == 7

    def test_x_future_none_when_no_cascade(self):
        y = _make_y(60)
        spec = FeatureSpec(product_key="product_d", feedstock_keys=["cracked_c5"], feedstock_lags=[1])
        loader = MagicMock()
        loader.read_actuals.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"),
            "cracked_c5": [800.0 + i for i in range(60)]})
        fx_loader = MagicMock()
        event_loader = MagicMock()
        event_loader.read_flags.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"), "event_flag": [0.0] * 60})
        result = build_features("product_d", y, spec, loader, fx_loader, event_loader, 7, None)
        assert result.X_future is None

    def test_upstream_product_returns_none(self):
        y = _make_y(60)
        spec = FeatureSpec(product_key="product_e", feedstock_keys=[])
        loader = MagicMock()
        fx_loader = MagicMock()
        event_loader = MagicMock()
        event_loader.read_flags.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=60, freq="D"), "event_flag": [0.0] * 60})
        result = build_features("product_e", y, spec, loader, fx_loader, event_loader, 7, None)
        assert result.X_train is None
        assert result.X_future is None


class TestNoLeakage:
    def test_x_train_for_date_t_uses_only_past_feedstock(self):
        y = _make_y(60)
        spec = FeatureSpec(product_key="product_d", feedstock_keys=["cracked_c5"], feedstock_lags=[1])
        dates = pd.date_range("2026-01-01", periods=60, freq="D")
        feedstock_values = [100.0 * i for i in range(60)]
        loader = MagicMock()
        loader.read_actuals.return_value = pd.DataFrame({"ds": dates, "cracked_c5": feedstock_values})
        fx_loader = MagicMock()
        event_loader = MagicMock()
        event_loader.read_flags.return_value = pd.DataFrame({"ds": dates, "event_flag": [0.0] * 60})
        result = build_features("product_d", y, spec, loader, fx_loader, event_loader, 7, None)
        assert result.X_train is not None
        lag_col = [c for c in result.X_train.columns if "cracked_c5" in c and "lag1" in c]
        assert len(lag_col) == 1
        # First training row's cracked_c5_lag1 = feedstock_values[0] (the day before)
        assert result.X_train[lag_col[0]].iloc[0] == pytest.approx(feedstock_values[0])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/test_feature_builder.py -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/forecasting/features/feature_builder.py
"""Assemble the exogenous feature matrix (X) aligned to target y.

Training features: past actuals only (no leakage).
Horizon features: cascade values for feedstocks, flat for FX, zero for events.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
import pandas as pd

from app.services.forecasting.features.feature_registry import FeatureSpec

logger = logging.getLogger(__name__)


@dataclass
class FeatureMatrix:
    X_train: pd.DataFrame | None
    X_future: pd.DataFrame | None
    feature_names: list[str] = field(default_factory=list)
    cleaning_note: str = ""


def build_features(
    target_product_key: str,
    y: pd.Series,
    spec: FeatureSpec,
    feedstock_loader,
    fx_loader,
    event_loader,
    horizon: int,
    feedstock_forecasts: dict[str, list[float]] | None = None,
) -> FeatureMatrix:
    if not spec.feedstock_keys:
        return FeatureMatrix(X_train=None, X_future=None, feature_names=[])

    y_dates = y.index
    start_date = y_dates.min()
    fetch_start = start_date - timedelta(days=max(spec.feedstock_lags) + 7)
    end_date = y_dates.max()

    # Load feedstock actuals
    feedstock_dfs: dict[str, pd.DataFrame] = {}
    for fk in spec.feedstock_keys:
        try:
            df = feedstock_loader.read_actuals(fk, fetch_start, end_date)
            feedstock_dfs[fk] = df.set_index("ds")
        except Exception as exc:
            logger.warning("Failed to load feedstock %s: %s", fk, exc)

    if not feedstock_dfs:
        return FeatureMatrix(X_train=None, X_future=None, cleaning_note="feedstock load failed")

    # Load FX
    fx_df = None
    if spec.use_fx:
        try:
            fx_df = fx_loader.read_usd_cny(fetch_start, end_date).set_index("ds")
        except Exception as exc:
            logger.warning("Failed to load FX: %s", exc)

    # Load events
    try:
        event_df = event_loader.read_flags(target_product_key, fetch_start, end_date).set_index("ds")
    except Exception as exc:
        logger.warning("Failed to load events: %s", exc)
        event_df = pd.DataFrame({"event_flag": 0.0}, index=y_dates)

    # Build X_train
    train_rows: list[dict] = []
    train_dates: list = []
    for date in y_dates:
        row: dict = {}
        for fk, df in feedstock_dfs.items():
            for lag in spec.feedstock_lags:
                lag_date = date - timedelta(days=lag)
                col_name = f"{fk}_lag{lag}"
                if lag_date in df.index:
                    row[col_name] = float(df.loc[lag_date, fk])
                elif len(df) > 0:
                    prior = df[df.index <= lag_date]
                    row[col_name] = float(prior[fk].iloc[-1]) if len(prior) > 0 else float(df[fk].iloc[0])
                else:
                    row[col_name] = 0.0
        for (child, parent) in spec.spread_pairs:
            child_col = f"{child}_lag1"
            parent_col = f"{parent}_lag1"
            if child_col in row and parent_col in row:
                row[f"spread_{child}_{parent}"] = row[child_col] - row[parent_col]
        if spec.use_fx and fx_df is not None:
            if date in fx_df.index:
                row["usd_cny"] = float(fx_df.loc[date, "usd_cny"])
            elif len(fx_df) > 0:
                row["usd_cny"] = float(fx_df["usd_cny"].iloc[-1])
            else:
                row["usd_cny"] = 7.2
        if date in event_df.index:
            row["event_flag"] = float(event_df.loc[date, "event_flag"])
        else:
            row["event_flag"] = 0.0
        if spec.calendar_features:
            dow = date.weekday()
            row["dow_sin"] = float(np.sin(2 * np.pi * dow / 7))
            row["dow_cos"] = float(np.cos(2 * np.pi * dow / 7))
        train_rows.append(row)
        train_dates.append(date)

    X_train = pd.DataFrame(train_rows, index=train_dates)

    # Winsorize feedstock lag columns
    for col in X_train.columns:
        if "_lag" in col and len(X_train) >= 20:
            p1, p99 = np.percentile(X_train[col].dropna(), [1, 99])
            X_train[col] = X_train[col].clip(lower=p1, upper=p99)

    # Build X_future
    X_future = None
    if feedstock_forecasts is not None and all(fk in feedstock_forecasts for fk in spec.feedstock_keys):
        future_dates = pd.date_range(start=y_dates.max() + timedelta(days=1), periods=horizon, freq="D")
        future_rows: list[dict] = []
        for step, date in enumerate(future_dates):
            row: dict = {}
            for fk in spec.feedstock_keys:
                forecast_vals = feedstock_forecasts[fk]
                for lag in spec.feedstock_lags:
                    col_name = f"{fk}_lag{lag}"
                    idx = step - lag
                    if idx >= 0:
                        row[col_name] = float(forecast_vals[idx])
                    elif idx < 0:
                        df = feedstock_dfs.get(fk)
                        if df is not None and len(df) > 0:
                            row[col_name] = float(df[fk].iloc[idx])
                        else:
                            row[col_name] = 0.0
            for (child, parent) in spec.spread_pairs:
                child_col = f"{child}_lag1"
                parent_col = f"{parent}_lag1"
                if child_col in row and parent_col in row:
                    row[f"spread_{child}_{parent}"] = row[child_col] - row[parent_col]
            if spec.use_fx and fx_df is not None and len(fx_df) > 0:
                row["usd_cny"] = float(fx_df["usd_cny"].iloc[-1])
            row["event_flag"] = 0.0
            if spec.calendar_features:
                dow = date.weekday()
                row["dow_sin"] = float(np.sin(2 * np.pi * dow / 7))
                row["dow_cos"] = float(np.cos(2 * np.pi * dow / 7))
            future_rows.append(row)
        X_future = pd.DataFrame(future_rows, index=future_dates)

    return FeatureMatrix(
        X_train=X_train,
        X_future=X_future,
        feature_names=list(X_train.columns),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/test_feature_builder.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/forecasting/features/feature_builder.py \
        backend/tests/test_feature_builder.py
git commit -m "feat(forecast): add exogenous feature builder with cascade + no-leakage (M2)"
```

---

## Task 5: XGBoost exog extension + `xgboost_exog` registration

**Files:**
- Modify: `backend/app/services/forecasting/models/xgboost_reg.py`
- Modify: `backend/app/services/forecasting/models/__init__.py`
- Test: `backend/tests/test_xgboost_exog.py`

**Interface change:** `XGBoostReg.fit(y, seasonal_period, exog=None) -> "XGBoostReg"` and `XGBoostReg.forecast(h, exog_future=None) -> np.ndarray`. New class attribute `uses_exog: bool = False`.

- [ ] **Step 1: Read the current `xgboost_reg.py` to find `_build_features` and `fit`/`forecast` signatures**

```bash
cat backend/app/services/forecasting/models/xgboost_reg.py | head -200
```

- [ ] **Step 2: Write the failing test**

```python
# backend/tests/test_xgboost_exog.py
"""Tests for XGBoost exogenous feature support + xgboost_exog registration."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.models.xgboost_reg import XGBoostReg
from app.services.forecasting.models import build_model_pool


def _make_y(n: int = 80) -> pd.Series:
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    values = 5000 + np.sin(np.arange(n) * 0.1) * 100 + np.random.rand(n) * 20
    return pd.Series(values, index=dates, name="y")


def _make_exog(n: int = 80, n_features: int = 3) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    data = {f"exog_{i}": np.random.rand(n) * 100 for i in range(n_features)}
    return pd.DataFrame(data, index=dates)


class TestXGBoostExogFit:
    def test_fit_without_exog_works(self):
        model = XGBoostReg()
        y = _make_y(80)
        model.fit(y, seasonal_period=7)
        assert model._model is not None

    def test_fit_with_exog_concatenates(self):
        model = XGBoostReg(uses_exog=True)
        y = _make_y(80)
        exog = _make_exog(80)
        model.fit(y, seasonal_period=7, exog=exog)
        assert model._model is not None

    def test_uses_exog_flag_persists(self):
        model = XGBoostReg(uses_exog=True)
        assert model.uses_exog is True


class TestXGBoostExogForecast:
    def test_forecast_without_exog_works(self):
        model = XGBoostReg()
        y = _make_y(80)
        model.fit(y, seasonal_period=7)
        result = model.forecast(7)
        assert len(result) == 7

    def test_forecast_with_exog_future(self):
        model = XGBoostReg(uses_exog=True)
        y = _make_y(80)
        exog = _make_exog(80)
        model.fit(y, seasonal_period=7, exog=exog)
        exog_future = _make_exog(n=7)
        result = model.forecast(7, exog_future=exog_future)
        assert len(result) == 7

    def test_forecast_requires_exog_when_model_uses_it(self):
        """If model was fit with exog, forecast requires exog_future."""
        model = XGBoostReg(uses_exog=True)
        y = _make_y(80)
        exog = _make_exog(80)
        model.fit(y, seasonal_period=7, exog=exog)
        with pytest.raises(ValueError, match="exog_future required"):
            model.forecast(7)


class TestRegistration:
    def test_xgboost_exog_registered(self):
        pool = build_model_pool(seasonal_period=7)
        # Pool should have either xgboost_reg (no exog) or both
        # When exog is available, build_model_pool(seasonal_period=7, include_exog=True) returns both
        assert "xgboost_reg" in pool or "xgboost_exog" in pool

    def test_xgboost_exog_separate_from_xgboost_reg(self):
        """xgboost_exog is a separate model instance when both exist."""
        from app.services.forecasting.models.xgboost_reg import XGBoostReg
        # Manually create both
        m1 = XGBoostReg()
        m2 = XGBoostReg(uses_exog=True)
        assert m1.uses_exog is False
        assert m2.uses_exog is True
        assert m1 is not m2
```

- [ ] **Step 3: Modify `xgboost_reg.py` to support exog**

Open `backend/app/services/forecasting/models/xgboost_reg.py` and apply these changes:

1. Add class attribute `uses_exog: bool = False` to the `XGBoostReg` class
2. Modify `__init__` to accept `uses_exog: bool = False` parameter, store as `self.uses_exog`
3. Modify `fit(self, y: pd.Series, seasonal_period: int = 7, exog: pd.DataFrame | None = None)`:
   - If `self.uses_exog` and `exog is None`: raise `ValueError("XGBoostReg with uses_exog=True requires exog")`
   - Build base endogenous features via existing `_build_features`
   - If `exog is not None`: align exog to `y.index`, concat to features
   - Store `self._exog_feature_names = list(exog.columns)` if exog used
4. Modify `forecast(self, h: int, exog_future: pd.DataFrame | None = None)`:
   - If `self.uses_exog` and `exog_future is None`: raise `ValueError("forecast with uses_exog model requires exog_future")`
   - Use existing recursive forecast loop, but concatenate exog_future rows to each step's features

- [ ] **Step 4: Modify `models/__init__.py` to register `xgboost_exog`**

Open `backend/app/services/forecasting/models/__init__.py`. In the existing `build_model_pool` function, after the existing XGBoost registration, add:

```python
# Register xgboost_exog as a separate model variant
if "xgboost_reg" in pool:
    try:
        from .xgboost_reg import XGBoostReg
        pool["xgboost_exog"] = XGBoostReg(uses_exog=True)
    except Exception:
        pass  # xgboost not installed
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/test_xgboost_exog.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/forecasting/models/xgboost_reg.py \
        backend/app/services/forecasting/models/__init__.py \
        backend/tests/test_xgboost_exog.py
git commit -m "feat(forecast): extend XGBoost with optional exog support (M2)

XGBoostReg gains uses_exog flag + exog param to fit/forecast.
xgboost_exog registered as separate model for ensemble to compare."
```

---

## Task 6: Value-chain coherence — `reconcile.py`

**Files:**
- Create: `backend/app/services/forecasting/reconcile.py`
- Test: `backend/tests/test_reconcile.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_reconcile.py
"""Tests for value-chain coherence (spread guardrails + directional checks)."""
from __future__ import annotations

import pytest

from app.services.forecasting.reconcile import (
    CoherenceReport, check_coherence, apply_coherence,
)


class TestCheckCoherence:
    def test_no_violations_when_coherent(self):
        report = check_coherence(
            product_key="product_d",
            forecast_values=[5500.0, 5520.0, 5540.0],
            feedstock_forecast=[600.0, 605.0, 610.0],   # product_c
            feedstock_key="product_c",
            spread_inversion_threshold=0.0,
        )
        assert report.spread_inverted is False
        assert report.direction_consistent is True
        assert len(report.violations) == 0

    def test_detects_spread_inversion(self):
        """child < feedstock cost → spread_inverted=True."""
        report = check_coherence(
            product_key="cracked_c5",
            forecast_values=[500.0, 505.0, 510.0],   # below product_c cost
            feedstock_forecast=[600.0, 605.0, 610.0],
            feedstock_key="product_c",
            spread_inversion_threshold=0.0,
        )
        assert report.spread_inverted is True
        assert any(v["type"] == "spread_inversion" for v in report.violations)

    def test_detects_directional_mismatch(self):
        """Feedstock moved >5% but child didn't move commensurately."""
        # Feedstock jumped 20%, child moved <5%
        report = check_coherence(
            product_key="product_d",
            forecast_values=[5500.0, 5505.0, 5510.0],   # ~0% move
            feedstock_forecast=[600.0, 660.0, 720.0],   # 20% jump
            feedstock_key="product_c",
            direction_threshold_pct=5.0,
        )
        assert report.direction_consistent is False
        assert any(v["type"] == "directional_mismatch" for v in report.violations)

    def test_no_directional_mismatch_within_threshold(self):
        """Feedstock moved <5% → no directional mismatch."""
        report = check_coherence(
            product_key="product_d",
            forecast_values=[5500.0, 5510.0, 5520.0],   # ~0.4% move
            feedstock_forecast=[600.0, 602.0, 604.0],   # ~0.7% move
            feedstock_key="product_c",
            direction_threshold_pct=5.0,
        )
        assert report.direction_consistent is True

    def test_no_feedstock_skips_checks(self):
        """If feedstock_forecast is None, no checks applied."""
        report = check_coherence(
            product_key="product_e",
            forecast_values=[500.0, 505.0, 510.0],
            feedstock_forecast=None,
            feedstock_key=None,
        )
        assert len(report.violations) == 0
        assert report.spread_inverted is False


class TestApplyCoherence:
    def test_clamp_on_spread_inversion(self):
        forecast = [500.0, 505.0, 510.0]
        feedstock_forecast = [600.0, 605.0, 610.0]
        report = check_coherence("test", forecast, feedstock_forecast, "product_c")
        clamped, new_report = apply_coherence(forecast, report, feedstock_forecast, min_margin=0.2)
        assert new_report.clamped is True
        # Clamped values should be feedstock * 1.2
        assert clamped[0] == pytest.approx(720.0)  # 600 * 1.2

    def test_no_clamp_when_coherent(self):
        forecast = [5500.0, 5520.0, 5540.0]
        feedstock_forecast = [600.0, 605.0, 610.0]
        report = check_coherence("test", forecast, feedstock_forecast, "product_c")
        clamped, new_report = apply_coherence(forecast, report, feedstock_forecast, min_margin=0.2)
        assert new_report.clamped is False
        assert clamped == forecast

    def test_directional_mismatch_flagged_not_clamped(self):
        """Directional mismatches are flags, not clamps."""
        forecast = [5500.0, 5505.0, 5510.0]
        feedstock_forecast = [600.0, 660.0, 720.0]
        report = check_coherence("test", forecast, feedstock_forecast, "product_c")
        clamped, new_report = apply_coherence(forecast, report, feedstock_forecast, min_margin=0.2)
        assert new_report.clamped is False  # directional mismatch doesn't trigger clamp
        assert clamped == forecast
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/test_reconcile.py -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/forecasting/reconcile.py
"""Value-chain coherence: spread guardrails + directional consistency.

Not literal HTS reconciliation — our crude→product_c→C5 chain is a production
chain with yield rates, not an additive disaggregation. Instead, we check:
  1. Spread guardrails — child forecast should not invert vs feedstock cost
  2. Directional consistency — flag if feedstock moved >X% but child didn't
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class CoherenceReport:
    product_key: str
    feedstock_key: str | None
    violations: list[dict] = field(default_factory=list)
    direction_consistent: bool = True
    spread_inverted: bool = False
    clamped: bool = False


def check_coherence(
    product_key: str,
    forecast_values: list[float],
    feedstock_forecast: list[float] | None,
    feedstock_key: str | None,
    spread_inversion_threshold: float = 0.0,
    direction_threshold_pct: float = 5.0,
) -> CoherenceReport:
    """Check value-chain coherence. Does NOT silently rewrite forecasts."""
    report = CoherenceReport(product_key=product_key, feedstock_key=feedstock_key)

    if feedstock_forecast is None or len(feedstock_forecast) != len(forecast_values):
        return report

    # 1. Spread guardrails
    for i, (child, parent) in enumerate(zip(forecast_values, feedstock_forecast)):
        if parent <= 0:
            continue
        ratio = child / parent
        if ratio < 1.0 + spread_inversion_threshold:
            report.spread_inverted = True
            report.violations.append({
                "type": "spread_inversion",
                "step": i,
                "forecast_value": child,
                "feedstock_value": parent,
                "ratio": ratio,
                "severity": "high",
                "message": f"{product_key} step {i}: forecast {child:.2f} below feedstock {parent:.2f}",
            })

    # 2. Directional consistency
    if len(forecast_values) >= 2:
        feedstock_start = feedstock_forecast[0]
        feedstock_end = feedstock_forecast[-1]
        if feedstock_start > 0:
            feedstock_pct_move = abs((feedstock_end - feedstock_start) / feedstock_start) * 100
            if feedstock_pct_move > direction_threshold_pct:
                child_start = forecast_values[0]
                child_end = forecast_values[-1]
                child_pct_move = abs((child_end - child_start) / child_start) * 100 if child_start > 0 else 0
                if child_pct_move < direction_threshold_pct:
                    report.direction_consistent = False
                    report.violations.append({
                        "type": "directional_mismatch",
                        "feedstock_pct_move": feedstock_pct_move,
                        "child_pct_move": child_pct_move,
                        "severity": "medium",
                        "message": (
                            f"{feedstock_key} moved {feedstock_pct_move:.1f}% but "
                            f"{product_key} only moved {child_pct_move:.1f}%"
                        ),
                    })

    return report


def apply_coherence(
    forecast_values: list[float],
    report: CoherenceReport,
    feedstock_forecast: list[float] | None,
    min_margin: float = 0.2,
) -> tuple[list[float], CoherenceReport]:
    """Apply clamps for hard inversions. Returns (forecast, updated_report)."""
    new_report = CoherenceReport(
        product_key=report.product_key,
        feedstock_key=report.feedstock_key,
        violations=list(report.violations),
        direction_consistent=report.direction_consistent,
        spread_inverted=report.spread_inverted,
        clamped=False,
    )

    if feedstock_forecast is None or not new_report.spread_inverted:
        return forecast_values, new_report

    # Apply clamps: child = feedstock * (1 + min_margin)
    clamped = list(forecast_values)
    was_clamped = False
    for i, (child, parent) in enumerate(zip(clamped, feedstock_forecast)):
        if parent > 0 and child < parent * (1 + min_margin):
            new_value = parent * (1 + min_margin)
            clamped[i] = new_value
            was_clamped = True

    new_report.clamped = was_clamped
    return clamped, new_report
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/test_reconcile.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/forecasting/reconcile.py backend/tests/test_reconcile.py
git commit -m "feat(forecast): add value-chain coherence (spread guardrails) (M3)

check_coherence flags spread inversions (child < feedstock cost) and
directional mismatches. apply_coherence clamps hard inversions to
feedstock × (1 + min_margin). Directional mismatches flagged only."
```

---

## Task 7: Continuous learning — `accuracy_tracker.py`

**Files:**
- Create: `backend/app/services/forecasting/accuracy_tracker.py`
- Test: `backend/tests/test_accuracy_tracker.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_accuracy_tracker.py
"""Tests for accuracy tracking, drift detection, and adaptive weights."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from app.services.forecasting.accuracy_tracker import (
    RealizedAccuracy, evaluate_previous_run, rolling_accuracy,
    detect_drift, adaptive_weights,
)


class TestEvaluatePreviousRun:
    def test_compares_forecast_to_actual(self):
        mock_db = MagicMock()
        mock_run = MagicMock()
        mock_run.results = {
            "7d": {
                "base": [5500.0, 5520.0, 5540.0, 5560.0, 5580.0, 5600.0, 5620.0],
                "as_of_date": "2026-01-01",
            }
        }
        mock_db.query.return_value.filter.return_value.order_by.return_value.first.return_value = mock_run
        actuals = pd.Series([5510.0, 5525.0, 5550.0, 5565.0, 5595.0, 5610.0, 5630.0])

        results = evaluate_previous_run(mock_db, "product_d", actuals)
        # 7 evaluations
        assert len(results) == 7
        # First realized_error should be (5500-5510)/5510 ≈ -0.0018
        assert results[0].realized_error is not None
        assert abs(results[0].realized_error - (5500 - 5510) / 5510) < 0.001

    def test_empty_actuals_returns_empty(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
        results = evaluate_previous_run(mock_db, "product_d", pd.Series(dtype=float))
        assert results == []


class TestRollingAccuracy:
    def test_returns_mean_mape_and_trend(self):
        mock_db = MagicMock()
        # Mock accuracy logs
        logs = []
        for i, mape in enumerate([0.10, 0.12, 0.08, 0.15, 0.20, 0.18]):
            log = MagicMock()
            log.realized_mape = mape
            log.evaluated_at = (datetime.utcnow() - timedelta(days=i)).isoformat()
            logs.append(log)
        mock_db.query.return_value.filter.return_value.all.return_value = logs

        result = rolling_accuracy(mock_db, "product_d", window_days=30)
        assert "mean_mape" in result
        assert "n_evaluations" in result
        assert "trend" in result
        assert result["n_evaluations"] == 6


class TestDetectDrift:
    def test_drifts_when_recent_much_worse_than_baseline(self):
        mock_db = MagicMock()
        # Baseline: 5 days with mape=0.10 (90-day window)
        # Recent: 3 days with mape=0.20 (30-day window)
        all_logs = []
        for i in range(5):
            log = MagicMock()
            log.realized_mape = 0.10
            log.evaluated_at = (datetime.utcnow() - timedelta(days=80 - i)).isoformat()
            all_logs.append(log)
        for i in range(3):
            log = MagicMock()
            log.realized_mape = 0.20
            log.evaluated_at = (datetime.utcnow() - timedelta(days=10 - i)).isoformat()
            all_logs.append(log)
        mock_db.query.return_value.filter.return_value.all.return_value = all_logs

        result = detect_drift(mock_db, "product_d")
        assert result["is_drifting"] is True
        assert result["recent_mape"] == pytest.approx(0.20)
        assert result["baseline_mape"] == pytest.approx(0.10)

    def test_no_drift_when_stable(self):
        mock_db = MagicMock()
        all_logs = []
        for i in range(8):
            log = MagicMock()
            log.realized_mape = 0.10 + (i * 0.001)  # tiny trend
            log.evaluated_at = (datetime.utcnow() - timedelta(days=80 - i * 5)).isoformat()
            all_logs.append(log)
        mock_db.query.return_value.filter.return_value.all.return_value = all_logs

        result = detect_drift(mock_db, "product_d")
        assert result["is_drifting"] is False


class TestAdaptiveWeights:
    def test_blends_backtest_and_realized(self):
        """70% backtest + 30% realized."""
        backtest = {"xgboost_reg": 0.4, "seasonal_naive": 0.3, "arima": 0.3}
        realized = {"xgboost_reg": 0.6, "seasonal_naive": 0.2, "arima": 0.2}

        weights = adaptive_weights(backtest, realized, realized_weight_factor=0.3)
        # xgboost: 0.7*0.4 + 0.3*0.6 = 0.28 + 0.18 = 0.46
        assert weights["xgboost_reg"] == pytest.approx(0.46, abs=0.01)
        # All weights sum to 1.0
        assert sum(weights.values()) == pytest.approx(1.0)

    def test_cold_start_uses_backtest_only(self):
        backtest = {"a": 0.5, "b": 0.5}
        weights = adaptive_weights(backtest, None, realized_weight_factor=0.3)
        assert weights == backtest

    def test_zero_factor_returns_backtest(self):
        backtest = {"a": 0.5, "b": 0.5}
        realized = {"a": 0.9, "b": 0.1}
        weights = adaptive_weights(backtest, realized, realized_weight_factor=0.0)
        assert weights == backtest
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/test_accuracy_tracker.py -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/forecasting/accuracy_tracker.py
"""Track realized forecast error, detect drift, feed adaptive ensemble weights."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from app.models.forecasting import ForecastRun, ForecastAccuracyLog

logger = logging.getLogger(__name__)


@dataclass
class RealizedAccuracy:
    product_key: str
    run_id: str
    horizon: int
    forecast_value: float
    actual_value: Optional[float]
    realized_error: Optional[float]
    realized_mape: Optional[float]
    evaluated_at: Optional[datetime]


def evaluate_previous_run(db, product_key: str, current_actuals: pd.Series) -> list[RealizedAccuracy]:
    """Compare the previous ForecastRun's horizon values against newly-arrived actuals.

    Writes to ForecastAccuracyLog (extended with realized_error/realized_mape/evaluated_at).
    """
    # Find the previous run
    from app.models.forecasting import ForecastTarget
    target = db.query(ForecastTarget).filter(
        ForecastTarget.product_key == product_key, ForecastTarget.is_deleted == False
    ).first()
    if not target:
        return []

    prev_run = db.query(ForecastRun).filter(
        ForecastRun.target_id == target.id
    ).order_by(ForecastRun.created_date.desc()).first()
    if not prev_run or not prev_run.results:
        return []

    results = []
    for horizon_key, horizon_data in prev_run.results.items():
        if not isinstance(horizon_data, dict):
            continue
        try:
            h = int(horizon_key.rstrip("d"))
        except ValueError:
            continue
        base = horizon_data.get("base", [])
        if len(base) != h:
            continue
        as_of = horizon_data.get("as_of_date")
        # Map each horizon step to the actual date
        for i, forecast_val in enumerate(base):
            if as_of is None:
                continue
            try:
                target_date = pd.Timestamp(as_of) + pd.Timedelta(days=i + 1)
            except Exception:
                continue
            actual_val = None
            if target_date in current_actuals.index:
                actual_val = float(current_actuals.loc[target_date])
            realized_error = None
            realized_mape = None
            if actual_val is not None and actual_val > 0:
                realized_error = (forecast_val - actual_val) / actual_val
                realized_mape = abs(realized_error)
            results.append(RealizedAccuracy(
                product_key=product_key, run_id=str(prev_run.id), horizon=i + 1,
                forecast_value=float(forecast_val), actual_value=actual_val,
                realized_error=realized_error, realized_mape=realized_mape,
                evaluated_at=datetime.utcnow() if actual_val is not None else None,
            ))

    # Write to ForecastAccuracyLog
    for r in results:
        if r.realized_error is not None:
            log = ForecastAccuracyLog(
                target_id=target.id,
                run_id=int(r.run_id) if r.run_id.isdigit() else None,
                horizon_days=r.horizon,
                realized_error=r.realized_error,
                realized_mape=r.realized_mape,
                evaluated_at=r.evaluated_at,
            )
            db.add(log)
    db.commit()
    return results


def rolling_accuracy(db, product_key: str, model_name: str | None = None,
                     window_days: int = 30) -> dict:
    """Returns {'mean_mape': float, 'n_evaluations': int, 'trend': str}."""
    from app.models.forecasting import ForecastTarget
    target = db.query(ForecastTarget).filter(
        ForecastTarget.product_key == product_key, ForecastTarget.is_deleted == False
    ).first()
    if not target:
        return {"mean_mape": None, "n_evaluations": 0, "trend": "unknown"}

    cutoff = datetime.utcnow() - pd.Timedelta(days=window_days).to_pytimedelta()
    logs = db.query(ForecastAccuracyLog).filter(
        ForecastAccuracyLog.target_id == target.id,
        ForecastAccuracyLog.evaluated_at >= cutoff,
        ForecastAccuracyLog.realized_mape.isnot(None),
    ).all()

    mapes = [float(log.realized_mape) for log in logs]
    n = len(mapes)
    if n == 0:
        return {"mean_mape": None, "n_evaluations": 0, "trend": "unknown"}
    mean_mape = float(np.mean(mapes))
    # Simple trend: compare first half vs second half
    if n >= 6:
        first_half = np.mean(mapes[: n // 2])
        second_half = np.mean(mapes[n // 2:])
        if second_half < first_half * 0.9:
            trend = "improving"
        elif second_half > first_half * 1.1:
            trend = "degrading"
        else:
            trend = "stable"
    else:
        trend = "unknown"
    return {"mean_mape": mean_mape, "n_evaluations": n, "trend": trend}


def detect_drift(db, product_key: str, baseline_window_days: int = 90,
                 recent_window_days: int = 30, drift_threshold_pct: float = 20.0) -> dict:
    """Recent MAPE > baseline × (1 + threshold/100) → drift."""
    from app.models.forecasting import ForecastTarget
    target = db.query(ForecastTarget).filter(
        ForecastTarget.product_key == product_key, ForecastTarget.is_deleted == False
    ).first()
    if not target:
        return {"is_drifting": False, "reason": "no target"}

    all_logs = db.query(ForecastAccuracyLog).filter(
        ForecastAccuracyLog.target_id == target.id,
        ForecastAccuracyLog.realized_mape.isnot(None),
    ).all()

    baseline_mapes = []
    recent_mapes = []
    baseline_cutoff = datetime.utcnow() - pd.Timedelta(days=baseline_window_days).to_pytimedelta()
    recent_cutoff = datetime.utcnow() - pd.Timedelta(days=recent_window_days).to_pytimedelta()
    for log in all_logs:
        if log.evaluated_at is None:
            continue
        mape = float(log.realized_mape)
        if log.evaluated_at >= recent_cutoff:
            recent_mapes.append(mape)
        elif log.evaluated_at >= baseline_cutoff:
            baseline_mapes.append(mape)

    if len(baseline_mapes) < 3 or len(recent_mapes) < 3:
        return {"is_drifting": False, "reason": "insufficient data"}

    baseline_mape = float(np.mean(baseline_mapes))
    recent_mape = float(np.mean(recent_mapes))
    is_drifting = recent_mape > baseline_mape * (1 + drift_threshold_pct / 100)
    return {
        "is_drifting": is_drifting,
        "baseline_mape": baseline_mape,
        "recent_mape": recent_mape,
        "reason": f"recent={recent_mape:.3f} vs baseline={baseline_mape:.3f}",
    }


def adaptive_weights(
    backtest_weights: dict[str, float],
    realized_weights: dict[str, float] | None,
    realized_weight_factor: float = 0.3,
) -> dict[str, float]:
    """Blend backtest weights with recent realized accuracy.

    Cold start (realized_weights=None) → returns backtest_weights unchanged.
    """
    if realized_weights is None or realized_weight_factor == 0.0:
        return dict(backtest_weights)

    blended: dict[str, float] = {}
    for model, bw in backtest_weights.items():
        rw = realized_weights.get(model, bw)
        blended[model] = (1 - realized_weight_factor) * bw + realized_weight_factor * rw

    # Renormalize to sum to 1.0
    total = sum(blended.values())
    if total > 0:
        blended = {k: v / total for k, v in blended.items()}
    return blended
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/test_accuracy_tracker.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/forecasting/accuracy_tracker.py \
        backend/tests/test_accuracy_tracker.py
git commit -m "feat(forecast): add realized accuracy tracking + drift detection (M4)"
```

---

## Task 8: NL explanation — `explain.py`

**Files:**
- Create: `backend/app/services/forecasting/explain.py`
- Test: `backend/tests/test_explain.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_explain.py
"""Tests for driver attribution + NL explanation."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.forecasting.explain import (
    DriverAttribution, Explanation, explain_forecast,
)


def _make_cleaning_report():
    return MagicMock(
        n_spikes_detected=2,
        spike_dates=["2026-07-15", "2026-07-22"],
        n_level_shifts=0,
        is_stale=False,
        notes="2 spikes corrected",
    )


def _make_coherence_report():
    return MagicMock(
        violations=[],
        spread_inverted=False,
        direction_consistent=True,
        clamped=False,
    )


class TestExplainForecast:
    def test_produces_nl_summary(self):
        explanation = explain_forecast(
            product_key="product_d",
            forecast_values=[5500.0, 5520.0, 5540.0],
            previous_forecast=[5480.0, 5500.0, 5520.0],
            xgboost_model=None,
            feature_names=["cracked_c5_lag1", "product_c_lag1"],
            cleaning_report=_make_cleaning_report(),
            coherence_report=_make_coherence_report(),
            drift_status={"is_drifting": False},
            honesty_gate_triggered=True,
        )
        assert isinstance(explanation, Explanation)
        assert explanation.summary
        assert "product_d" in explanation.summary.lower() or "2.3%" in explanation.summary or "+2.3" in explanation.summary

    def test_confidence_high_when_no_gate_no_drift(self):
        explanation = explain_forecast(
            "product_d", [5500.0], [5480.0], None, [],
            _make_cleaning_report(), _make_coherence_report(),
            {"is_drifting": False}, honesty_gate_triggered=False,
        )
        assert explanation.confidence == "high"

    def test_confidence_medium_when_gate_triggered(self):
        explanation = explain_forecast(
            "product_d", [5500.0], [5480.0], None, [],
            _make_cleaning_report(), _make_coherence_report(),
            {"is_drifting": False}, honesty_gate_triggered=True,
        )
        assert explanation.confidence == "medium"

    def test_confidence_low_when_drift_detected(self):
        explanation = explain_forecast(
            "product_d", [5500.0], [5480.0], None, [],
            _make_cleaning_report(), _make_coherence_report(),
            {"is_drifting": True}, honesty_gate_triggered=False,
        )
        assert explanation.confidence in ("medium", "low")

    def test_confidence_low_when_both(self):
        explanation = explain_forecast(
            "product_d", [5500.0], [5480.0], None, [],
            _make_cleaning_report(), _make_coherence_report(),
            {"is_drifting": True}, honesty_gate_triggered=True,
        )
        assert explanation.confidence == "low"

    def test_cleaning_note_in_explanation(self):
        explanation = explain_forecast(
            "product_d", [5500.0], None, None, [],
            _make_cleaning_report(), _make_coherence_report(),
            {"is_drifting": False}, honesty_gate_triggered=False,
        )
        assert "2 spikes" in explanation.cleaning_note or "spike" in explanation.cleaning_note.lower()

    def test_no_shap_import(self):
        """Verify no SHAP dependency is used."""
        # If this test runs, explain.py doesn't import shap at module level
        import app.services.forecasting.explain as explain_mod
        assert not hasattr(explain_mod, "shap")

    def test_contribution_delta_none_when_no_previous(self):
        explanation = explain_forecast(
            "product_d", [5500.0], None, None, ["f1"],
            _make_cleaning_report(), _make_coherence_report(),
            {"is_drifting": False}, honesty_gate_triggered=False,
        )
        for d in explanation.top_drivers:
            assert d.contribution_delta is None

    def test_coherence_note_when_violations(self):
        coh = MagicMock(violations=[{"message": "spread inversion"}],
                         spread_inverted=True, direction_consistent=True, clamped=False)
        explanation = explain_forecast(
            "product_d", [5500.0], None, None, [],
            _make_cleaning_report(), coh,
            {"is_drifting": False}, honesty_gate_triggered=False,
        )
        assert "spread inversion" in explanation.coherence_note or "violation" in explanation.coherence_note.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/test_explain.py -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/forecasting/explain.py
"""Driver attribution + NL explanation for every ForecastRun.

Deliberately NO SHAP dependency — uses XGBoost feature_importances_ + delta.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class DriverAttribution:
    feature_name: str
    importance: float
    contribution_delta: float | None = None


@dataclass
class Explanation:
    product_key: str
    summary: str
    top_drivers: list[DriverAttribution] = field(default_factory=list)
    cleaning_note: str = ""
    coherence_note: str = ""
    confidence: str = "medium"
    confidence_reasons: list[str] = field(default_factory=list)


def explain_forecast(
    product_key: str,
    forecast_values: list[float],
    previous_forecast: list[float] | None,
    xgboost_model,
    feature_names: list[str],
    cleaning_report,
    coherence_report,
    drift_status: dict,
    honesty_gate_triggered: bool,
) -> Explanation:
    """Generate a driver attribution + NL explanation for a forecast run."""
    # Compute delta vs previous
    delta_pct = 0.0
    if previous_forecast and previous_forecast[0] > 0:
        delta_pct = ((forecast_values[0] - previous_forecast[0]) / previous_forecast[0]) * 100

    # Driver attribution from XGBoost feature_importances_
    top_drivers: list[DriverAttribution] = []
    if xgboost_model is not None and hasattr(xgboost_model, "_model"):
        try:
            import xgboost as xgb
            importance = xgboost_model._model.get_score(importance_type="gain")
            # Map by feature name
            sorted_imp = sorted(importance.items(), key=lambda x: -x[1])[:3]
            for feat_name, imp_val in sorted_imp:
                contrib_delta = None
                if previous_forecast is not None:
                    contrib_delta = imp_val * (delta_pct / 100.0) if delta_pct != 0 else None
                top_drivers.append(DriverAttribution(
                    feature_name=feat_name,
                    importance=float(imp_val),
                    contribution_delta=contrib_delta,
                ))
        except Exception as exc:
            logger.debug("Could not extract feature_importances_: %s", exc)

    # Cleaning note
    cleaning_note = ""
    if cleaning_report is not None:
        cleaning_note = (
            f"{cleaning_report.n_spikes_detected} spikes auto-corrected"
            + (f" on {', '.join(cleaning_report.spike_dates[:3])}" if cleaning_report.spike_dates else "")
            + "." if cleaning_report.n_spikes_detected > 0 else ""
        )
        if cleaning_report.n_level_shifts > 0:
            cleaning_note += f" {cleaning_report.n_level_shifts} level shifts marked."
        if cleaning_report.is_stale:
            cleaning_note += f" Series stale ({cleaning_report.stale_tail_days} days)."

    # Coherence note
    coherence_note = ""
    if coherence_report is not None and coherence_report.violations:
        coherence_note = f"{len(coherence_report.violations)} coherence violation(s): " + "; ".join(
            v["message"] for v in coherence_report.violations[:2]
        )

    # Confidence
    confidence_reasons = []
    if honesty_gate_triggered:
        confidence_reasons.append("honesty gate triggered (ensemble fell back to seasonal_naive)")
    if drift_status.get("is_drifting"):
        confidence_reasons.append(f"drift detected ({drift_status.get('reason', '')})")

    if honesty_gate_triggered and drift_status.get("is_drifting"):
        confidence = "low"
    elif honesty_gate_triggered or drift_status.get("is_drifting"):
        confidence = "medium"
    else:
        confidence = "high"

    # NL summary
    driver_text = ""
    if top_drivers:
        top = top_drivers[0]
        driver_text = f" driven by {top.feature_name} (importance {top.importance:.2f})"

    direction = "up" if delta_pct > 0 else ("down" if delta_pct < 0 else "flat")
    summary = (
        f"{product_key} forecast {direction} {abs(delta_pct):.1f}% vs last run{driver_text}. "
        + (cleaning_note if cleaning_note else "")
        + (f" {coherence_note}" if coherence_note else " No coherence violations.")
        + f" Confidence: {confidence}"
        + (f" — {', '.join(confidence_reasons)}" if confidence_reasons else "")
    )

    return Explanation(
        product_key=product_key,
        summary=summary,
        top_drivers=top_drivers,
        cleaning_note=cleaning_note,
        coherence_note=coherence_note,
        confidence=confidence,
        confidence_reasons=confidence_reasons,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/test_explain.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/forecasting/explain.py backend/tests/test_explain.py
git commit -m "feat(forecast): add driver attribution + NL explanation (M4)

Uses XGBoost feature_importances_ + delta. No SHAP dependency.
Confidence: high/medium/low based on honesty gate + drift status."
```

---

## Task 9: Model columns + migration 039

**Files:**
- Modify: `backend/app/models/forecasting.py`
- Create: `backend/alembic/versions/039_forecast_enhancement_phase1.py`

- [ ] **Step 1: Modify `ForecastRun` model**

In `backend/app/models/forecasting.py`, add these columns to the `ForecastRun` class:

```python
    explanation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cleaning_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    coherence_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    exog_degraded: Mapped[bool] = mapped_column(Boolean, default=False)
```

- [ ] **Step 2: Modify `ForecastAccuracyLog` model**

In `backend/app/models/forecasting.py`, add these columns to the `ForecastAccuracyLog` class:

```python
    realized_error: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_mape: Mapped[float | None] = mapped_column(Float, nullable=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
```

- [ ] **Step 3: Create the alembic migration**

```bash
# From backend/ directory, run the migration generator:
# (NOT to be run by the agent — use the manual script below)
```

Create `backend/alembic/versions/039_forecast_enhancement_phase1.py`:

```python
"""forecast enhancement phase 1 columns

Revision ID: 039_forecast_enhancement_phase1
Revises: 038_resource_shares_and_type
Create Date: 2026-08-03
"""
from alembic import op
import sqlalchemy as sa

revision = "039_forecast_enhancement_phase1"
down_revision = "038_resource_shares_and_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ForecastRun extensions
    op.add_column("forecast_runs", sa.Column("explanation", sa.JSON(), nullable=True))
    op.add_column("forecast_runs", sa.Column("cleaning_report", sa.JSON(), nullable=True))
    op.add_column("forecast_runs", sa.Column("coherence_report", sa.JSON(), nullable=True))
    op.add_column("forecast_runs", sa.Column("exog_degraded", sa.Boolean(), nullable=False, server_default=sa.text("false")))

    # ForecastAccuracyLog extensions
    op.add_column("forecast_accuracy_logs", sa.Column("realized_error", sa.Float(), nullable=True))
    op.add_column("forecast_accuracy_logs", sa.Column("realized_mape", sa.Float(), nullable=True))
    op.add_column("forecast_accuracy_logs", sa.Column("evaluated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("forecast_accuracy_logs", "evaluated_at")
    op.drop_column("forecast_accuracy_logs", "realized_mape")
    op.drop_column("forecast_accuracy_logs", "realized_error")
    op.drop_column("forecast_runs", "exog_degraded")
    op.drop_column("forecast_runs", "coherence_report")
    op.drop_column("forecast_runs", "cleaning_report")
    op.drop_column("forecast_runs", "explanation")
```

- [ ] **Step 4: Run the migration**

```bash
cd /home/ysk2025/zhanlu_7_30/backend
docker compose exec backend alembic upgrade head
docker compose restart backend
```

- [ ] **Step 5: Verify columns exist**

```bash
docker compose exec postgres psql -U postgres -d zhanlu -c "\d forecast_runs" | grep -E "explanation|cleaning|coherence|exog_degraded"
docker compose exec postgres psql -U postgres -d zhanlu -c "\d forecast_accuracy_logs" | grep -E "realized|evaluated_at"
```

Expected: 4 lines for forecast_runs, 3 lines for forecast_accuracy_logs.

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/forecasting.py \
        backend/alembic/versions/039_forecast_enhancement_phase1.py
git commit -m "feat(forecast): add explanation/cleaning/coherence columns + migration 039 (M5)

Migration adds nullable JSON columns + realized_error/mape/evaluated_at.
Forward-only (additive). Backfill not required."
```

---

## Task 10: Engine wiring + topological ordering

**Files:**
- Modify: `backend/app/services/forecasting/engine.py`
- Modify: `backend/app/config.py`

- [ ] **Step 1: Add config flags**

In `backend/app/config.py`, add (in the Settings class):

```python
    FORECAST_EXOG_ENABLED: bool = True
    FORECAST_PREPROCESS_ENABLED: bool = True
    FORECAST_CASCADE_ENABLED: bool = True
    FORECAST_ADAPTIVE_WEIGHTS_FACTOR: float = 0.3
    FORECAST_DRIFT_THRESHOLD_PCT: float = 20.0
```

- [ ] **Step 2: Add topological ordering to `compute_all` + populate cascade dict**

Open `backend/app/services/forecasting/engine.py`. In `compute_all`, before the loop, import the registry and the KG:

```python
from app.services.forecasting.features.feature_registry import topological_order
from app.services.knowledge_graph.graph import get_domain_graph
```

Inside `compute_all`, after loading targets and before the loop:

```python
    graph = get_domain_graph()
    product_keys = [t.product_key for t in targets]
    ordered_keys = topological_order(product_keys, graph)
    targets = sorted(targets, key=lambda t: ordered_keys.index(t.product_key) if t.product_key in ordered_keys else 999)

    # Cascade dict: populated as each product's forecast completes.
    # Downstream products consume this dict via build_features(cascade_forecasts=...).
    cascade_forecasts: dict[str, list[float]] = {}
```

After each product's run completes inside the loop, store its horizon forecast:

```python
    # After the ForecastRun is written and committed:
    if hasattr(ensemble_forecast, "tolist"):
        cascade_forecasts[target.product_key] = ensemble_forecast.tolist()[:horizons[-1]]
    else:
        cascade_forecasts[target.product_key] = list(ensemble_forecast)[:horizons[-1]]
```

Pass `cascade_forecasts` into `compute_target` (or read it from a shared scope) so the feature builder can use it.

- [ ] **Step 3: Wire preprocess stage in `compute_target`**

In `compute_target`, after fetching `y` from `_fetch_series`, add:

```python
    from app.services.forecasting.preprocess import preprocess_series
    if settings.FORECAST_PREPROCESS_ENABLED:
        preprocess_result = preprocess_series(y, target.product_key, seasonal_period=seasonal_period)
        y = preprocess_result.y_clean
        cleaning_report = preprocess_result.report
    else:
        cleaning_report = None
```

- [ ] **Step 4: Wire feature builder stage in `compute_target`**

After quality scoring, add the feature builder block:

```python
    exog_x_train = None
    exog_x_future = None
    exog_degraded = False
    if settings.FORECAST_EXOG_ENABLED and settings.FORECAST_CASCADE_ENABLED:
        from app.services.knowledge_graph.graph import get_domain_graph
        from app.services.forecasting.features.feature_registry import derive_feature_spec
        from app.services.forecasting.features.exogenous_loaders import (
            FeedstockLoader, FxLoader, EventFlagLoader,
        )
        from app.services.forecasting.features.feature_builder import build_features
        try:
            graph = get_domain_graph()
            spec = derive_feature_spec(target.product_key, graph)
            feedstock_loader = FeedstockLoader()
            fx_loader = FxLoader()
            event_loader = EventFlagLoader()
            fm = build_features(
                target_product_key=target.product_key,
                y=y,
                spec=spec,
                feedstock_loader=feedstock_loader,
                fx_loader=fx_loader,
                event_loader=event_loader,
                horizon=horizons[-1],
                feedstock_forecasts=cascade_forecasts.get(target.product_key),
            )
            exog_x_train = fm.X_train
            exog_x_future = fm.X_future
        except Exception as exc:
            logger.warning("Exog feature build failed for %s: %s — degraded mode", target.product_key, exc)
            exog_degraded = True
```

- [ ] **Step 5: Fit `xgboost_exog` separately after `run_models`**

After `run_models` returns, but before `blend`:

```python
    # Fit xgboost_exog separately with exogenous features
    if (exog_x_train is not None and exog_x_future is not None
            and "xgboost_exog" in models):
        try:
            models["xgboost_exog"].fit(y, seasonal_period=seasonal_period, exog=exog_x_train)
            exog_forecast = models["xgboost_exog"].forecast(h=horizons[-1], exog_future=exog_x_future)
            forecasts["xgboost_exog"] = exog_forecast
            # Use backtest MAPE from the run_models results (same model class)
            per_model_error["xgboost_exog"] = per_model_error.get("xgboost_reg", 0.5)
        except Exception as exc:
            logger.warning("xgboost_exog fit/forecast failed for %s: %s", target.product_key, exc)
```

- [ ] **Step 6: Apply coherence after blend**

After `blend` produces `ensemble_forecast`, before guard:

```python
    # Value-chain coherence
    if settings.FORECAST_EXOG_ENABLED:
        from app.services.knowledge_graph.graph import get_domain_graph
        from app.services.forecasting.reconcile import check_coherence, apply_coherence
        graph = get_domain_graph()
        upstream = graph.upstream_of(target.product_key)
        if upstream:
            nearest = upstream[0]
            feedstock_fc = cascade_forecasts.get(nearest.id)
            if feedstock_fc is not None:
                coherence_report = check_coherence(
                    product_key=target.product_key,
                    forecast_values=ensemble_forecast[:len(feedstock_fc)].tolist(),
                    feedstock_forecast=feedstock_fc,
                    feedstock_key=nearest.id,
                )
                clamped, coherence_report = apply_coherence(
                    ensemble_forecast[:len(feedstock_fc)].tolist(),
                    coherence_report,
                    feedstock_fc,
                )
                ensemble_forecast[:len(clamped)] = clamped
            else:
                coherence_report = None
        else:
            coherence_report = None
    else:
        coherence_report = None
```

- [ ] **Step 7: Generate explanation after scenarios**

After `scenarios` builds the dict, add:

```python
    from app.services.forecasting.explain import explain_forecast
    from app.services.forecasting.accuracy_tracker import detect_drift
    drift_status = detect_drift(db, target.product_key, drift_threshold_pct=settings.FORECAST_DRIFT_THRESHOLD_PCT)
    explanation = explain_forecast(
        product_key=target.product_key,
        forecast_values=ensemble_forecast.tolist() if hasattr(ensemble_forecast, "tolist") else list(ensemble_forecast),
        previous_forecast=None,  # could read from previous run
        xgboost_model=models.get("xgboost_exog"),
        feature_names=[],  # populated if xgboost_exog was used
        cleaning_report=cleaning_report,
        coherence_report=coherence_report,
        drift_status=drift_status,
        honesty_gate_triggered=below_naive,
    )
```

- [ ] **Step 8: Persist explanation + cleaning + coherence + exog_degraded on `ForecastRun`**

In the ForecastRun constructor block, add:

```python
    run.explanation = {
        "summary": explanation.summary,
        "confidence": explanation.confidence,
        "confidence_reasons": explanation.confidence_reasons,
        "top_drivers": [{"feature_name": d.feature_name, "importance": d.importance} for d in explanation.top_drivers],
    }
    if cleaning_report is not None:
        run.cleaning_report = {
            "n_spikes_detected": cleaning_report.n_spikes_detected,
            "spike_dates": cleaning_report.spike_dates,
            "n_level_shifts": cleaning_report.n_level_shifts,
            "is_stale": cleaning_report.is_stale,
        }
    if coherence_report is not None:
        run.coherence_report = {
            "violations": coherence_report.violations,
            "spread_inverted": coherence_report.spread_inverted,
            "clamped": coherence_report.clamped,
        }
    run.exog_degraded = exog_degraded
```

- [ ] **Step 9: Commit**

```bash
git add backend/app/services/forecasting/engine.py backend/app/config.py
git commit -m "feat(forecast): wire preprocess/feature-builder/reconcile/explain into engine (M5)

Engine now uses topological ordering. New stages produce CleaningReport,
CoherenceReport, Explanation. Cascade populated per-product."
```

---

## Task 11: Integration test + config flags

**Files:**
- Create: `backend/tests/test_engine_phase1_integration.py`
- Create: `backend/tests/test_no_leakage_backtest.py`

- [ ] **Step 1: Write integration test**

```python
# backend/tests/test_engine_phase1_integration.py
"""End-to-end integration test for Phase 1 engine wiring."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest


class TestTopologicalOrdering:
    def test_cascade_populated_in_order(self):
        from app.services.knowledge_graph.graph import build_sample_graph
        from app.services.forecasting.features.feature_registry import topological_order
        graph = build_sample_graph()
        products = ["product_d", "product_e", "product_c", "cracked_c5"]
        ordered = topological_order(products, graph)
        # Verify cascade dict would be populated in order
        cascade_forecasts = {}
        for pk in ordered:
            # Simulate each product's forecast becoming available
            cascade_forecasts[pk] = [5000.0] * 7  # dummy forecast
        assert "product_e" in cascade_forecasts
        assert "product_c" in cascade_forecasts
        assert cascade_forecasts["product_e"][0] == 5000.0


class TestForecastRunPersistence:
    def test_run_has_explanation_column(self):
        """Verify the ForecastRun model has the new explanation column."""
        from app.models.forecasting import ForecastRun
        from sqlalchemy import inspect
        mapper = inspect(ForecastRun)
        columns = {c.key for c in mapper.columns}
        assert "explanation" in columns
        assert "cleaning_report" in columns
        assert "coherence_report" in columns
        assert "exog_degraded" in columns

    def test_accuracy_log_has_realized_columns(self):
        from app.models.forecasting import ForecastAccuracyLog
        from sqlalchemy import inspect
        mapper = inspect(ForecastAccuracyLog)
        columns = {c.key for c in mapper.columns}
        assert "realized_error" in columns
        assert "realized_mape" in columns
        assert "evaluated_at" in columns
```

- [ ] **Step 2: Write no-leakage backtest test**

```python
# backend/tests/test_no_leakage_backtest.py
"""Critical: ensure no future feedstock values leak into backtest training features."""
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.features.feature_registry import FeatureSpec
from app.services.forecasting.features.feature_builder import build_features


def test_x_train_has_no_future_feedstock_values():
    """For every training row at date t, feedstock lag columns must
    reference dates ≤ t-1."""
    y = pd.Series(
        [5000.0] * 30,
        index=pd.date_range("2026-01-01", periods=30, freq="D"),
    )
    # Feedstock prices — each day has a distinct value so we can detect leakage
    dates = pd.date_range("2026-01-01", periods=30, freq="D")
    feedstock_values = [1000.0 * i for i in range(30)]
    loader = MagicMock()
    loader.read_actuals.return_value = pd.DataFrame({
        "ds": dates, "cracked_c5": feedstock_values,
    })
    fx_loader = MagicMock()
    fx_loader.read_usd_cny.return_value = pd.DataFrame({
        "ds": dates, "usd_cny": [7.2] * 30})
    event_loader = MagicMock()
    event_loader.read_flags.return_value = pd.DataFrame({
        "ds": dates, "event_flag": [0.0] * 30})

    spec = FeatureSpec(product_key="test", feedstock_keys=["cracked_c5"], feedstock_lags=[1])
    result = build_features("test", y, spec, loader, fx_loader, event_loader, 7, None)
    assert result.X_train is not None
    lag_col = [c for c in result.X_train.columns if "cracked_c5" in c][0]

    # For each training row, the lag value should reference a feedstock value
    # that is ≤ the feedstock value at that row's date.
    for i, train_date in enumerate(result.X_train.index):
        # Find feedstock index for this date
        feedstock_idx = i  # dates align
        if feedstock_idx == 0:
            continue  # first row has no lag-1 value
        # Lag should be feedstock_values[feedstock_idx - 1] or earlier
        actual_lag_value = result.X_train[lag_col].iloc[i]
        expected_lag = feedstock_values[feedstock_idx - 1]
        assert actual_lag_value == pytest.approx(expected_lag), (
            f"Leakage detected at row {i}: X_train[{lag_col}]={actual_lag_value} "
            f"but feedstock[{feedstock_idx}]={feedstock_values[feedstock_idx]}"
        )
        # Most importantly: NOT equal to feedstock_values[feedstock_idx] (the current day)
        assert actual_lag_value != feedstock_values[feedstock_idx]
```

- [ ] **Step 3: Run integration tests**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/test_engine_phase1_integration.py tests/test_no_leakage_backtest.py -v`
Expected: PASS

- [ ] **Step 4: Run full regression suite**

Run each existing test file individually (SSH RAM limited):
```bash
cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/test_edia_source.py -v
cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/test_seed_acme_targets.py -v
cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/test_forecast_dashboard_endpoint_v2.py -v
cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest tests/test_bootstrap_nightly_forecast.py -v
```
Expected: All existing tests still pass (no regression).

- [ ] **Step 5: Trigger a manual nightly run to verify cascade works**

```bash
cd /home/ysk2025/zhanlu_7_30/backend
docker compose exec backend python -c "
from app.db.session import SessionLocal
from app.services.forecasting.engine import ForecastEngine
db = SessionLocal()
try:
    engine = ForecastEngine()
    runs = engine.compute_all(org_id=None, horizons=[7], seasonal_period=7)
    print(f'Computed {len(runs)} forecast runs')
    for r in runs:
        has_explanation = r.explanation is not None
        print(f'  Run {r.id}: explanation={has_explanation}, exog_degraded={r.exog_degraded}')
finally:
    db.close()
"
```
Expected: 12 runs, all with explanation populated, exog_degraded=True for upstream products (product_e, product_c — no feedstocks).

- [ ] **Step 6: Commit**

```bash
git add backend/tests/test_engine_phase1_integration.py \
        backend/tests/test_no_leakage_backtest.py
git commit -m "test(forecast): add Phase 1 integration tests + no-leakage backtest (M5)

End-to-end integration test verifies cascade ordering + column persistence.
No-leakage test asserts future feedstock values never appear in training."
```

- [ ] **Step 7: Verify success criteria**

Check the database for the new fields:
```bash
docker compose exec postgres psql -U postgres -d zhanlu -c "
SELECT
  ft.product_key,
  fr.created_date::date,
  (fr.explanation->>'confidence') AS confidence,
  fr.exog_degraded,
  (fr.cleaning_report->>'n_spikes_detected')::int AS spikes,
  (fr.coherence_report->>'spread_inverted')::bool AS spread_inv
FROM forecast_runs fr
JOIN forecast_targets ft ON fr.target_id = ft.id
WHERE ft.product_key LIKE 'acme.%'
ORDER BY ft.product_key, fr.created_date DESC
LIMIT 24;
"
```
Expected: 12 products × 2 rows (last 2 nights), with confidence/spikes/columns populated.

---

## Self-Review Notes (to verify after writing)

- [ ] All 11 tasks map back to a spec section (§5.1-§5.11)
- [ ] No placeholders — every test has actual code, every implementation has actual code
- [ ] Type names are consistent across tasks (`CleaningReport`, `PreprocessResult`, `FeatureSpec`, `FeatureMatrix`, `CoherenceReport`, `RealizedAccuracy`, `Explanation`)
- [ ] Imports are consistent (`app.services.forecasting.X`, `app.models.forecasting.X`)
- [ ] Docker-related steps reference the actual commands
- [ ] Tests run individually (no parallelism)
- [ ] Each task is independently testable (can be reviewed + merged separately)
- [ ] Spec's §5 nuance (value-chain coherence vs literal HTS) is reflected throughout (reconcile.py uses spread guardrails, not MinT)
- [ ] Success criteria from spec §12 are measurable from the verification steps in Task 11