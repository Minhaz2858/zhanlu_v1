# Wave 0 — F1: Loader Framework + Data Stores Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three data assets the forecast engine and AI Brief currently ignore — ERP sales **volume** (the `qty` column), **supplier price dispersion** (per-supplier daily prices from `md_t_lz_price`), and a **calibrated event-impact store** (schema for Wave 2's T2.2) — plus pure derived-metric functions and new evidence-pack fields that surface a real 供需研判 signal.

**Architecture:** New loaders follow the existing `exogenous_loaders.py` pattern (`.load()` → `pd.DataFrame`, indexed by date, no future leakage). Derived demand metrics live in a new pure module `demand_signal.py` (no I/O) so they are trivially unit-testable and reusable by both the evidence pack (brief) and, later, `feature_builder.py` (XGBoost exogenous). A new `ForecastEventImpact` model creates the calibration table that Wave 2's T2.2 populates. The evidence pack gains `demand` and `supplier_ladder` fields; the brief rewrite that *consumes* them is deferred to T1.1.

**Tech Stack:** Python 3.11, SQLAlchemy 2.0 (raw `text()` SQL for MySQL reads, ORM for the new PG table), pandas, pytest (run via `backend/venv/bin/python -m pytest`).

## Global Constraints

- **Backend venv**: run all tests with `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest <path> -v`. The system has no pytest.
- **No future leakage**: every loader returns only rows with `date <= window_end`. Mirrors `FeedstockLoader` / `ErpTxLoader`.
- **Sentinel handling**: drop `_SENTINEL_VALUES = {"F7","NaN","nan","None","","null","NULL"}` before numeric coercion, matching `edia_source.py`.
- **Schema management**: new ORM tables are created by `Base.metadata.create_all()` at startup (project convention — Alembic is not the source of truth; migration 026 crashes). Do NOT write an Alembic migration.
- **Chinese market colors**: 涨=red, 跌=green (applies to later brief work, not this plan).
- **MySQL engine**: `from app.core.mysql_db import get_mysql_engine` returns the EDIA MySQL engine (`EDIA_MYSQL_URL` @ 10.10.10.49:3306). Loaders tolerate `None` engine → return empty DataFrame.
- **Docker**: after touching `models/forecasting.py` (new table) restart with `docker restart zhanlu-backend`. No `--reload`.
- **Test isolation**: use SQLite in-memory or monkeypatched engines for loader tests; never require live MySQL in unit tests.

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `backend/app/services/forecasting/features/exogenous_loaders.py` | Hosts `ErpVolumeLoader` + `SupplierDispersionLoader` alongside existing `FeedstockLoader`/`ErpTxLoader` | Modify (append classes) |
| `backend/app/services/forecasting/demand_signal.py` | Pure derived demand metrics (4wk rolling volume, YoY vol change, vol-price divergence) — no I/O | Create |
| `backend/app/models/forecasting.py` | Add `ForecastEventImpact` ORM model (calibration table for T2.2) | Modify (append class) |
| `backend/app/models/__init__.py` | Export new model so `create_all` registers it | Modify (append import) |
| `backend/app/services/forecasting/analyst/evidence_pack.py` | Add `demand` + `supplier_ladder` fields to the pack dict | Modify |
| `backend/tests/test_forecasting/test_demand_signal.py` | Unit tests for pure derived metrics | Create |
| `backend/tests/test_forecasting/test_loaders_volume_supplier.py` | Unit tests for new loaders (monkeypatched engine) | Create |
| `backend/tests/test_forecasting/test_event_impact_model.py` | Unit test for new ORM model round-trip on SQLite | Create |
| `backend/tests/test_forecasting/test_evidence_pack_fields.py` | Unit test that pack includes new fields with correct shape | Create |

---

### Task 1: ErpVolumeLoader — read ERP sales `qty` column

**Files:**
- Modify: `backend/app/services/forecasting/features/exogenous_loaders.py` (append after `ErpTxLoader`, line ~309)
- Test: `backend/tests/test_forecasting/test_loaders_volume_supplier.py`

**Interfaces:**
- Produces: `ErpVolumeLoader.load(erp_table, product_filter, window_start, window_end) -> pd.DataFrame` with column `erp_volume` (daily sum of `qty`), indexed by date. Empty DataFrame on failure/no engine.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_forecasting/test_loaders_volume_supplier.py
"""Tests for ErpVolumeLoader and SupplierDispersionLoader."""
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.services.forecasting.features.exogenous_loaders import (
    ErpVolumeLoader, SupplierDispersionLoader,
)


def _fake_engine(rows):
    """Build a mock engine whose connect() returns the given rows."""
    engine = MagicMock()
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = rows
    engine.connect.return_value.__enter__ = lambda self: conn
    engine.connect.return_value.__exit__ = lambda *a: None
    return engine


class TestErpVolumeLoader:
    def test_load_returns_daily_volume_sum(self):
        rows = [
            ("2026-01-01", 100.0),
            ("2026-01-01", 50.0),   # two transactions same day -> sum 150
            ("2026-01-02", 200.0),
        ]
        loader = ErpVolumeLoader(engine=_fake_engine(rows))
        df = loader.load(
            erp_table="sale_erp_v_异戊二烯_data",
            product_filter="异戊二烯",
            window_start=date(2026, 1, 1),
            window_end=date(2026, 1, 2),
        )
        assert list(df.columns) == ["erp_volume"]
        assert df.loc[pd.Timestamp("2026-01-01"), "erp_volume"] == 150.0
        assert df.loc[pd.Timestamp("2026-01-02"), "erp_volume"] == 200.0

    def test_load_empty_when_no_engine(self):
        loader = ErpVolumeLoader(engine=None)
        with patch(
            "app.services.forecasting.features.exogenous_loaders._resolve_mysql_engine",
            return_value=None,
        ):
            df = loader.load("t", "p", date(2026, 1, 1), date(2026, 1, 2))
        assert df.empty

    def test_load_drops_sentinel_qty(self):
        rows = [
            ("2026-01-01", "F7"),     # sentinel -> dropped
            ("2026-01-01", 100.0),
        ]
        loader = ErpVolumeLoader(engine=_fake_engine(rows))
        df = loader.load("t", "", date(2026, 1, 1), date(2026, 1, 1))
        assert len(df) == 1
        assert df["erp_volume"].iloc[0] == 100.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecasting/test_loaders_volume_supplier.py::TestErpVolumeLoader -v`
Expected: FAIL — `ImportError: cannot import name 'ErpVolumeLoader'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/forecasting/features/exogenous_loaders.py`:

```python
# ---------------------------------------------------------------------------
# ERP Sales Volume Loader (Wave 0 F1)
# ---------------------------------------------------------------------------

class ErpVolumeLoader:
    """Loads ERP sales *volume* (the ``qty`` column) from ``sale_erp_v_{product}_data``.

    Sales volume is a demand leading indicator: order-flow softening tends to
    precede price softening by days/weeks. Unlike ``ErpTxLoader`` (which reads
    ``price``), this reads ``qty`` and aggregates by **sum** per day (total
    units transacted), not mean.

    Returns a DataFrame with column ``erp_volume`` (daily sum of ``qty``),
    indexed by date. Empty DataFrame on failure or no engine.
    """

    def __init__(self, engine: Engine | None = None):
        self._engine = engine

    @property
    def engine(self) -> Engine | None:
        return self._engine or _resolve_mysql_engine()

    def load(
        self,
        erp_table: str,
        product_filter: str,
        window_start: date,
        window_end: date,
    ) -> pd.DataFrame:
        if self.engine is None or not erp_table:
            return pd.DataFrame()

        where = "WHERE `date` >= :start AND `date` <= :end"
        params: dict = {
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
        }
        if product_filter:
            where += " AND `product` = :product"
            params["product"] = product_filter

        sql = text(
            f"SELECT `date` AS ds, `qty` AS y "
            f"FROM `{erp_table}` "
            f"{where} "
            f"ORDER BY `date` ASC"
        )
        try:
            with self.engine.connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except Exception as exc:
            logger.warning("ErpVolumeLoader failed for %s: %s", erp_table, exc)
            return pd.DataFrame()

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=["ds", "y"])
        df["ds"] = pd.to_datetime(df["ds"], errors="coerce")
        df = df[~df["y"].astype(str).isin(_SENTINEL_VALUES)]
        df["y"] = pd.to_numeric(df["y"], errors="coerce")
        df = df.dropna(subset=["ds", "y"])
        if df.empty:
            return pd.DataFrame()

        daily = df.groupby("ds")["y"].sum().rename("erp_volume")
        daily = daily.sort_index()
        mask = (daily.index >= pd.Timestamp(window_start)) & (
            daily.index <= pd.Timestamp(window_end)
        )
        return pd.DataFrame(daily.loc[mask])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecasting/test_loaders_volume_supplier.py::TestErpVolumeLoader -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/services/forecasting/features/exogenous_loaders.py backend/tests/test_forecasting/test_loaders_volume_supplier.py
git commit -m "feat(forecast): add ErpVolumeLoader to read ERP sales qty column"
```

---

### Task 2: SupplierDispersionLoader — per-supplier price spread from `md_t_lz_price`

**Files:**
- Modify: `backend/app/services/forecasting/features/exogenous_loaders.py` (append after `ErpVolumeLoader`)
- Test: `backend/tests/test_forecasting/test_loaders_volume_supplier.py` (append `TestSupplierDispersionLoader`)

**Interfaces:**
- Consumes: `FMATERIAL_NAME` = `{product}-{supplier}` pattern from `md_t_lz_price`.
- Produces: `SupplierDispersionLoader.load(product_key, window_start, window_end) -> pd.DataFrame` with columns `min_price`, `max_price`, `mean_price`, `spread_pct`, `n_suppliers`, indexed by date. Empty DataFrame on failure/no engine.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_forecasting/test_loaders_volume_supplier.py`:

```python
class TestSupplierDispersionLoader:
    def test_load_returns_per_supplier_dispersion(self):
        # FMATERIAL_NAME = "{product}-{supplier}"
        rows = [
            ("2026-01-01", "异戊二烯-供应商A", 100.0),
            ("2026-01-01", "异戊二烯-供应商B", 120.0),
            ("2026-01-01", "异戊二烯-供应商C", 110.0),
            ("2026-01-02", "异戊二烯-供应商A", 105.0),
        ]
        engine = _fake_engine(rows)
        loader = SupplierDispersionLoader(engine=engine)
        df = loader.load("异戊二烯", date(2026, 1, 1), date(2026, 1, 2))
        assert set(df.columns) == {
            "min_price", "max_price", "mean_price", "spread_pct", "n_suppliers",
        }
        # Day 1: min=100, max=120, mean=110, spread=(120-100)/110=0.1818, n=3
        row1 = df.loc[pd.Timestamp("2026-01-01")]
        assert row1["min_price"] == 100.0
        assert row1["max_price"] == 120.0
        assert row1["mean_price"] == pytest.approx(110.0)
        assert row1["spread_pct"] == pytest.approx(0.1818, abs=0.01)
        assert row1["n_suppliers"] == 3
        # Day 2: single supplier -> spread 0
        assert df.loc[pd.Timestamp("2026-01-02"), "n_suppliers"] == 1

    def test_load_empty_when_no_rows(self):
        engine = _fake_engine([])
        loader = SupplierDispersionLoader(engine=engine)
        df = loader.load("异戊二烯", date(2026, 1, 1), date(2026, 1, 2))
        assert df.empty
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecasting/test_loaders_volume_supplier.py::TestSupplierDispersionLoader -v`
Expected: FAIL — `ImportError: cannot import name 'SupplierDispersionLoader'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/forecasting/features/exogenous_loaders.py`:

```python
# ---------------------------------------------------------------------------
# Supplier Price Dispersion Loader (Wave 0 F1)
# ---------------------------------------------------------------------------

class SupplierDispersionLoader:
    """Loads per-supplier price dispersion from ``md_t_lz_price``.

    ``FMATERIAL_NAME`` follows the ``{product}-{supplier}`` convention, so a
    single product has multiple rows per day (one per supplier). Dispersion
    (max-min spread, supplier count) is a market-tightness signal that fills
    the human reports' "价格梯队" gap with zero new data sources.

    Returns a DataFrame with columns ``min_price``, ``max_price``,
    ``mean_price``, ``spread_pct`` ((max-min)/mean), ``n_suppliers``,
    indexed by date.
    """

    def __init__(self, engine: Engine | None = None):
        self._engine = engine

    @property
    def engine(self) -> Engine | None:
        return self._engine or _resolve_mysql_engine()

    def load(
        self,
        product_key: str,
        window_start: date,
        window_end: date,
    ) -> pd.DataFrame:
        if self.engine is None or not product_key:
            return pd.DataFrame()

        sql = text(
            "SELECT FDATE AS ds, FMATERIAL_NAME AS mat, FTAXPRICE AS y "
            "FROM md_t_lz_price "
            "WHERE FMATERIAL_NAME LIKE :pattern "
            "AND FDATE >= :start AND FDATE <= :end "
            "ORDER BY FDATE ASC"
        )
        try:
            with self.engine.connect() as conn:
                rows = conn.execute(sql, {
                    "pattern": f"%{product_key}%",
                    "start": window_start.isoformat(),
                    "end": window_end.isoformat(),
                }).fetchall()
        except Exception as exc:
            logger.warning("SupplierDispersionLoader failed for %s: %s", product_key, exc)
            return pd.DataFrame()

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=["ds", "mat", "y"])
        df["ds"] = pd.to_datetime(df["ds"], errors="coerce")
        df = df[~df["y"].astype(str).isin(_SENTINEL_VALUES)]
        df["y"] = pd.to_numeric(df["y"], errors="coerce")
        df = df.dropna(subset=["ds", "y"])
        if df.empty:
            return pd.DataFrame()

        grouped = df.groupby("ds")["y"]
        out = pd.DataFrame({
            "min_price": grouped.min(),
            "max_price": grouped.max(),
            "mean_price": grouped.mean(),
            "n_suppliers": grouped.count().astype(int),
        })
        out["spread_pct"] = (
            (out["max_price"] - out["min_price"]) / out["mean_price"]
        ).where(out["mean_price"] != 0, 0.0)
        out = out.sort_index()
        mask = (out.index >= pd.Timestamp(window_start)) & (
            out.index <= pd.Timestamp(window_end)
        )
        return out.loc[mask]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecasting/test_loaders_volume_supplier.py::TestSupplierDispersionLoader -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/services/forecasting/features/exogenous_loaders.py backend/tests/test_forecasting/test_loaders_volume_supplier.py
git commit -m "feat(forecast): add SupplierDispersionLoader for per-supplier price spread"
```

---

### Task 3: `demand_signal.py` — pure derived demand metrics

**Files:**
- Create: `backend/app/services/forecasting/demand_signal.py`
- Test: `backend/tests/test_forecasting/test_demand_signal.py`

**Interfaces:**
- Consumes: a `pd.Series` of daily volume (from `ErpVolumeLoader`), optionally a `pd.Series` of daily price aligned on the same index.
- Produces:
  - `rolling_volume(volume: pd.Series, window: int = 28) -> pd.Series` — trailing N-day sum.
  - `yoy_volume_change(volume: pd.Series) -> pd.Series` — pct change vs same week last year (52-week lag), NaN where unavailable.
  - `volume_price_divergence(volume: pd.Series, price: pd.Series, window: int = 28) -> pd.Series` — +1 when volume falling but price rising (bearish divergence), -1 when opposite, 0 otherwise.
  - `summarize_demand(volume: pd.Series, price: pd.Series | None, as_of: pd.Timestamp | None) -> dict` — returns `{"vol_4w": float|None, "vol_yoy_pct": float|None, "divergence": int, "trend_zh": str}` for the evidence pack.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_forecasting/test_demand_signal.py
"""Tests for pure demand-signal derived metrics."""
import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.demand_signal import (
    rolling_volume, yoy_volume_change, volume_price_divergence, summarize_demand,
)


def _series(values, start="2025-01-01"):
    idx = pd.date_range(start, periods=len(values), freq="D")
    return pd.Series(values, index=idx, dtype=float)


class TestRollingVolume:
    def test_trailing_28d_sum(self):
        vol = _series([10.0] * 35)
        rv = rolling_volume(vol, window=28)
        # First 27 entries are partial sums; full window from index 27
        assert rv.iloc[27] == pytest.approx(280.0)
        assert rv.iloc[34] == pytest.approx(280.0)

    def test_empty_series(self):
        assert rolling_volume(pd.Series([], dtype=float)).empty


class TestYoyVolumeChange:
    def test_yoy_pct_vs_52_weeks_ago(self):
        vol = _series([100.0] * 400)
        yoy = yoy_volume_change(vol)
        # 52 weeks = 364 days; at index 364, prev=100, curr=100 -> 0%
        assert yoy.iloc[364] == pytest.approx(0.0, abs=1e-6)
        # Before 364 days, NaN
        assert pd.isna(yoy.iloc[100])


class TestVolumePriceDivergence:
    def test_bearish_divergence_volume_down_price_up(self):
        # Volume falling, price rising over last 28 days
        vol = _series(np.linspace(200, 100, 30))
        price = _series(np.linspace(1000, 1200, 30))
        div = volume_price_divergence(vol, price, window=28)
        assert div.iloc[-1] == 1  # bearish divergence

    def test_bullish_divergence_volume_up_price_down(self):
        vol = _series(np.linspace(100, 200, 30))
        price = _series(np.linspace(1200, 1000, 30))
        div = volume_price_divergence(vol, price, window=28)
        assert div.iloc[-1] == -1  # bullish divergence

    def test_no_divergence_when_aligned(self):
        vol = _series([100.0] * 30)
        price = _series([1000.0] * 30)
        div = volume_price_divergence(vol, price, window=28)
        assert div.iloc[-1] == 0


class TestSummarizeDemand:
    def test_returns_dict_with_expected_keys(self):
        vol = _series([100.0] * 40)
        price = _series([1000.0] * 40)
        s = summarize_demand(vol, price, as_of=vol.index[-1])
        assert set(s.keys()) == {"vol_4w", "vol_yoy_pct", "divergence", "trend_zh"}
        assert s["vol_4w"] == pytest.approx(2800.0)
        assert s["divergence"] == 0
        assert isinstance(s["trend_zh"], str)

    def test_trend_zh_bearish_when_divergence_positive(self):
        vol = _series(np.linspace(200, 100, 30))
        price = _series(np.linspace(1000, 1200, 30))
        s = summarize_demand(vol, price, as_of=vol.index[-1])
        assert s["divergence"] == 1
        assert "走弱" in s["trend_zh"]

    def test_none_when_insufficient_data(self):
        s = summarize_demand(pd.Series([], dtype=float), None, as_of=None)
        assert s["vol_4w"] is None
        assert s["vol_yoy_pct"] is None
        assert s["divergence"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecasting/test_demand_signal.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.forecasting.demand_signal'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/forecasting/demand_signal.py
"""Pure derived demand-signal metrics (no I/O).

Consumes daily volume (from ``ErpVolumeLoader``) and optionally daily price,
producing trailing-window and year-over-year demand indicators plus a
volume-price divergence flag. All functions are pure: they take pandas Series
and return pandas Series / dicts, so they are trivially unit-testable and
reusable by both the evidence pack (brief) and the feature builder (XGBoost).

Divergence convention:
    +1  bearish divergence — volume falling, price rising (demand softening)
    -1  bullish divergence — volume rising, price falling (demand strengthening)
     0  no divergence (aligned or insufficient data)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_WINDOW_DEFAULT = 28
_YOY_LAG_DAYS = 364  # 52 weeks


def rolling_volume(volume: pd.Series, window: int = _WINDOW_DEFAULT) -> pd.Series:
    """Trailing N-day sum of volume."""
    if volume is None or volume.empty:
        return pd.Series(dtype=float)
    return volume.rolling(window=window, min_periods=1).sum()


def yoy_volume_change(volume: pd.Series) -> pd.Series:
    """Pct change vs same day 52 weeks ago; NaN where unavailable."""
    if volume is None or volume.empty:
        return pd.Series(dtype=float)
    shifted = volume.shift(_YOY_LAG_DAYS)
    return (volume - shifted) / shifted.where(shifted != 0, np.nan)


def volume_price_divergence(
    volume: pd.Series,
    price: pd.Series,
    window: int = _WINDOW_DEFAULT,
) -> pd.Series:
    """Flag volume-price divergence over a trailing window.

    Uses the slope (first vs last) of the trailing window for each series.
    """
    if volume is None or volume.empty or price is None or price.empty:
        return pd.Series(0, index=volume.index if volume is not None else [])
    aligned = pd.concat([volume, price], axis=1, keys=["vol", "px"]).dropna()
    if aligned.empty:
        return pd.Series(0, index=volume.index)
    vol_slope = aligned["vol"].rolling(window=window, min_periods=window).apply(
        lambda s: s.iloc[-1] - s.iloc[0], raw=False
    )
    px_slope = aligned["px"].rolling(window=window, min_periods=window).apply(
        lambda s: s.iloc[-1] - s.iloc[0], raw=False
    )
    div = pd.Series(0, index=aligned.index)
    bearish = (vol_slope < 0) & (px_slope > 0)
    bullish = (vol_slope > 0) & (px_slope < 0)
    div[bearish] = 1
    div[bullish] = -1
    return div.reindex(volume.index).fillna(0).astype(int)


def summarize_demand(
    volume: pd.Series,
    price: pd.Series | None,
    as_of: pd.Timestamp | None,
    window: int = _WINDOW_DEFAULT,
) -> dict:
    """Return a summary dict for the evidence pack at ``as_of``."""
    if volume is None or volume.empty:
        return {"vol_4w": None, "vol_yoy_pct": None, "divergence": 0,
                "trend_zh": "无成交数据"}
    rv = rolling_volume(volume, window=window)
    yoy = yoy_volume_change(volume)
    idx = as_of if as_of is not None else volume.index[-1]
    vol_4w = float(rv.loc[idx]) if idx in rv.index and not pd.isna(rv.loc[idx]) else None
    vol_yoy = float(yoy.loc[idx]) if idx in yoy.index and not pd.isna(yoy.loc[idx]) else None
    divergence = 0
    trend_zh = "需求平稳"
    if price is not None and not price.empty:
        div = volume_price_divergence(volume, price, window=window)
        divergence = int(div.loc[idx]) if idx in div.index else 0
        if divergence == 1:
            trend_zh = "成交量走弱、价格走高，需求走弱迹象"
        elif divergence == -1:
            trend_zh = "成交量回升、价格走弱，需求改善迹象"
    return {
        "vol_4w": round(vol_4w, 2) if vol_4w is not None else None,
        "vol_yoy_pct": round(vol_yoy, 4) if vol_yoy is not None else None,
        "divergence": divergence,
        "trend_zh": trend_zh,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecasting/test_demand_signal.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/services/forecasting/demand_signal.py backend/tests/test_forecasting/test_demand_signal.py
git commit -m "feat(forecast): add pure demand_signal module for volume/divergence metrics"
```

---

### Task 4: `ForecastEventImpact` ORM model — calibration table for T2.2

**Files:**
- Modify: `backend/app/models/forecasting.py` (append class)
- Modify: `backend/app/models/__init__.py` (append import)
- Test: `backend/tests/test_forecasting/test_event_impact_model.py`

**Interfaces:**
- Produces: `ForecastEventImpact(TimestampedBase)` — table `forecast_event_impacts` with columns: `product_key` (indexed), `event_type` (indexed), `measured_impact_pct` (Float), `n_observations` (Int), `confidence` (String: low/medium/high), `window_end` (DateTime), `params` (JSON). UniqueConstraint on `(product_key, event_type)`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_forecasting/test_event_impact_model.py
"""Round-trip test for ForecastEventImpact ORM model on SQLite."""
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.base import TimestampedBase
from app.models.forecasting import ForecastEventImpact


def test_event_impact_round_trip():
    engine = create_engine("sqlite:///:memory:")
    TimestampedBase.metadata.create_all(engine, tables=[ForecastEventImpact.__table__])
    with Session(engine) as session:
        impact = ForecastEventImpact(
            product_key="isoprene",
            event_type="supply_disruption",
            measured_impact_pct=7.5,
            n_observations=12,
            confidence="medium",
            window_end=datetime(2026, 8, 5),
            params={"median": 7.1, "p25": 5.2, "p75": 9.8},
        )
        session.add(impact)
        session.commit()
        loaded = session.query(ForecastEventImpact).filter_by(
            product_key="isoprene", event_type="supply_disruption"
        ).one()
        assert loaded.measured_impact_pct == 7.5
        assert loaded.n_observations == 12
        assert loaded.params["p75"] == 9.8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecasting/test_event_impact_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'ForecastEventImpact'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/models/forecasting.py` (after `ForecastWeightAdjustment`):

```python
class ForecastEventImpact(TimestampedBase):
    """Calibrated event-impact estimates — replaces fixed caps in T2.2.

    Populated by an event-study job (Wave 2, T2.2) that measures the average
    realized price move after past events of each type per product. Until
    populated, ``intelligence_overlay.py`` falls back to its current fixed
    caps (±1.5/3.5/7.5/12%). One row per (product_key, event_type).
    """

    __tablename__ = "forecast_event_impacts"
    __table_args__ = (
        UniqueConstraint("product_key", "event_type", name="uq_event_impact_product_event"),
    )

    product_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # Average realized % move after historical events of this type/product
    measured_impact_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    n_observations: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # low / medium / high based on n_observations and dispersion
    confidence: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    window_end: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # JSON: {median, p25, p75, lookback_days, method}
    params: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
```

Then append to `backend/app/models/__init__.py` (add `ForecastEventImpact` to the existing import line from `forecasting`). First read the file to find the exact import line:

```bash
cd /home/ysk2025/zhanlu_7_30/backend && grep -n "from app.models.forecasting" app/models/__init__.py
```

Add `ForecastEventImpact` to that import tuple.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecasting/test_event_impact_model.py -v`
Expected: PASS

- [ ] **Step 5: Restart backend so `create_all` registers the new table**

Run: `docker restart zhanlu-backend`

- [ ] **Step 6: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/models/forecasting.py backend/app/models/__init__.py backend/tests/test_forecasting/test_event_impact_model.py
git commit -m "feat(forecast): add ForecastEventImpact model for T2.2 calibration store"
```

---

### Task 5: Evidence pack `demand` + `supplier_ladder` fields

**Files:**
- Modify: `backend/app/services/forecasting/analyst/evidence_pack.py` (extend `build_pack` signature + return dict)
- Test: `backend/tests/test_forecasting/test_evidence_pack_fields.py`

**Interfaces:**
- Consumes: `build_pack` gains two new kwargs: `demand_summary: dict | None = None` and `supplier_ladder: dict | None = None`. Both default `None` so existing callers are unaffected.
- Produces: the pack dict gains `"demand"` (the `summarize_demand` output or `None`) and `"supplier_ladder"` (`{"spread_pct", "n_suppliers", "min_price", "max_price"}` at the latest date or `None`).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_forecasting/test_evidence_pack_fields.py
"""Test that evidence pack includes demand + supplier_ladder fields."""
from app.services.forecasting.analyst.evidence_pack import build_pack


def _base_kwargs():
    return dict(
        product_id="isoprene", name_zh="异戊二烯", day=7,
        history_rows=[], upstream_histories={}, run_results={},
        model_detail=None, explanation={}, as_of_month=8,
    )


def test_pack_includes_demand_field_when_provided():
    demand = {"vol_4w": 2800.0, "vol_yoy_pct": -0.12, "divergence": 1,
              "trend_zh": "成交量走弱、价格走高，需求走弱迹象"}
    pack = build_pack(**_base_kwargs(), demand_summary=demand)
    assert pack["demand"] == demand


def test_pack_demand_none_when_not_provided():
    pack = build_pack(**_base_kwargs())
    assert pack["demand"] is None


def test_pack_includes_supplier_ladder_when_provided():
    ladder = {"spread_pct": 0.18, "n_suppliers": 3, "min_price": 100.0,
              "max_price": 120.0}
    pack = build_pack(**_base_kwargs(), supplier_ladder=ladder)
    assert pack["supplier_ladder"] == ladder


def test_pack_supplier_ladder_none_when_not_provided():
    pack = build_pack(**_base_kwargs())
    assert pack["supplier_ladder"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecasting/test_evidence_pack_fields.py -v`
Expected: FAIL — `TypeError: build_pack() got an unexpected keyword argument 'demand_summary'`

- [ ] **Step 3: Write minimal implementation**

In `backend/app/services/forecasting/analyst/evidence_pack.py`, modify the `build_pack` signature to add the two kwargs and append the two fields to the return dict:

```python
def build_pack(*, product_id, name_zh, day, history_rows, upstream_histories,
               run_results, model_detail, explanation, as_of_month,
               demand_summary=None, supplier_ladder=None) -> dict:
```

Then in the returned dict (after the `"thresholds"` key), add:

```python
        "demand": demand_summary,
        "supplier_ladder": supplier_ladder,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecasting/test_evidence_pack_fields.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run full evidence-pack + forecasting test suite to confirm no regressions**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecasting/ -v`
Expected: all PASS (existing tests unaffected by new optional kwargs)

- [ ] **Step 6: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/services/forecasting/analyst/evidence_pack.py backend/tests/test_forecasting/test_evidence_pack_fields.py
git commit -m "feat(forecast): add demand + supplier_ladder fields to evidence pack"
```

---

## Self-Review

**1. Spec coverage:**
- ERP volume demand signal data store → Task 1 (ErpVolumeLoader) + Task 3 (derived metrics) ✓
- Supplier price ladder data store → Task 2 (SupplierDispersionLoader) ✓
- Event-impact calibration table → Task 4 (ForecastEventImpact) ✓
- Loader framework pattern → Tasks 1–2 follow existing `exogenous_loaders.py` conventions ✓
- Evidence-pack fields that downstream brief/builder consume → Task 5 ✓
- Feature-builder XGBoost wiring → **deferred to T1.1** (correct — F1 is data stores only; brief rewrite is T1.1) ✓

**2. Placeholder scan:** No TBD/TODO/"add error handling" found. All steps have actual code.

**3. Type consistency:**
- `ErpVolumeLoader.load()` returns DataFrame with column `erp_volume` — consumed by `demand_signal.summarize_demand()` which takes a `pd.Series` (caller does `df["erp_volume"]`) ✓
- `SupplierDispersionLoader.load()` returns DataFrame with `min_price/max_price/mean_price/spread_pct/n_suppliers` — matches test assertions and the `supplier_ladder` dict shape ✓
- `summarize_demand` returns dict keys `{vol_4w, vol_yoy_pct, divergence, trend_zh}` — matches test and the `demand_summary` kwarg passed to `build_pack` ✓
- `ForecastEventImpact` field names (`product_key`, `event_type`, `measured_impact_pct`, `n_observations`, `confidence`, `window_end`, `params`) match test ✓
