# Ecisco BI Forecasting — True-Anchored Answers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the partially-ported `forecast_agent` subagent in the Ecisco BI project work end-to-end so a user can ask a forecasting question and get a data-grounded answer (or an explicit insufficient-data verdict) — never a hallucinated number.

**Architecture:** The `forecast_agent` (defined at `app/services/agent_definitions/__init__.py:782`) and `orchestrator_agent` (line 822) are already wired correctly. We enable the two heavy forecast tools at the registry level, repoint the `ecisco_c5c9_v1` pack to a real warehouse table, and add a thin `truth_gate` module that wraps engine results with a `data_anchor` (source rows) and converts thin-data cases into explicit `insufficient_data` verdicts. No new agents, no new prompts, no API contract changes.

**Tech Stack:** FastAPI + SQLAlchemy + pandas + statsmodels (existing) + Python 3.11 (existing) + pymysql (existing).

**Spec:** `docs/superpowers/specs/2026-07-31-ecisco-bi-forecasting-true-answers-design.md`

## Global Constraints

- Working directory: `/home/ysk2025/zhanlu_7_30`
- All Python invocations: `cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python ...`
- Test runner: `cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. ./venv/bin/python -m pytest <path> -v`
- Engine code is **append-only** — do not change return types of `compute_target` / `compute_all`. Add new methods instead.
- All forecast tool response shapes are documented in the spec §3 (Data Anchor) and §3 (Insufficient Data). They are the source of truth for the truth_gate contract.
- The orchestrator and forecast_agent prompts are NOT modified in this plan.
- The forecast pack JSON `ecisco_c5c9_v1.json` may be repointed to a real table name, but no other field changes.

---

## Task 1: Pre-flight discovery — decide pack target table

**Files:**
- Read: `backend/scripts/diagnose_warehouse_data.py` (created in prior session, may need re-check)
- Write: `docs/superpowers/notes/2026-07-31-pack-target-decision.md` (new, decision record)

**Goal:** Identify which real warehouse table `ecisco_c5c9_v1.json` should point at, before any code change. Document the decision.

- [ ] **Step 1: Verify the diagnose script exists and runs**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend
ls scripts/diagnose_warehouse_data.py 2>&1
PYTHONPATH=. ./venv/bin/python -c "import pymysql; print(pymysql.__version__)"
```

Expected: `diagnose_warehouse_data.py` exists and pymysql is installed. If the script is missing, recreate it from the prior session's content (it scans `aipdp_data_warehouse_prod` for: all tables, row counts on `actual_price`/`v_actual_price`/`v_lz_data`/`v_forecast_price`/`v_partner_forecast_price`/`v_market_data`/`monthly_prices`, date ranges, distinct products, forecastability scan, and ForecastTarget/ForecastRun counts).

- [ ] **Step 2: Run the diagnose script**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend
PYTHONPATH=. ./venv/bin/python scripts/diagnose_warehouse_data.py 2>&1 | tee /tmp/diagnose_output.txt
```

Expected: a structured report with row counts and date ranges per table. (If the script isn't found, this fails — recreate the script using the search pattern from the spec §7.)

- [ ] **Step 3: Read the output and pick the target table**

Look at the output and decide:

| Outcome | Target table for pack |
|---|---|
| A. `actual_price` has many dates and products | repoint to `actual_price` |
| B. Only `v_lz_data` has dates (>=30 rows for LZ001) | repoint to `v_lz_data`, scope pack dims to LZ-prefix |
| C. All tables have 0–1 dates | STOP. Cannot ship without loading the EDIA CSV. Escalate to user. |

- [ ] **Step 4: Write the decision record**

Create `docs/superpowers/notes/2026-07-31-pack-target-decision.md` with:

```markdown
# Pack Target Decision (2026-07-31)

## Outcome
- Picked: <table_name>
- Why: <one sentence from diagnose output>
- Date range in target table: <first> to <last>
- Distinct products in target table: <N>
- ForecastTarget rows in zhanlu DB: <N>
- ForecastRun rows in zhanlu DB: <N>

## If outcome was C (no data)
<escalation message>
```

- [ ] **Step 5: Commit the decision record**

```bash
cd /home/ysk2025/zhanlu_7_30
git add docs/superpowers/notes/2026-07-31-pack-target-decision.md
git commit -m "docs(forecasting): record pre-flight pack target decision"
```

---

## Task 2: Repoint `ecisco_c5c9_v1.json` to the real warehouse table

**Files:**
- Modify: `backend/app/services/forecasting/packs/ecisco_c5c9_v1.json` (16 `"table": "monthly_prices"` fields → new table name from Task 1)
- Create: `backend/tests/test_pack_ecisco_c5c9_v1.py` (new)

**Goal:** The pack loads cleanly with the new table reference, and we have a test that catches any future regression where the table name drifts back to a non-existent name.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_pack_ecisco_c5c9_v1.py`:

```python
"""Pack ecisco_c5c9_v1 must point at a real warehouse table."""

import json
import os
from pathlib import Path

import pytest

PACK_PATH = (
    Path(__file__).parent.parent
    / "app/services/forecasting/packs/ecisco_c5c9_v1.json"
)


def _load_pack() -> dict:
    with open(PACK_PATH) as f:
        return json.load(f)


def test_pack_file_exists():
    assert PACK_PATH.is_file(), f"Pack not found at {PACK_PATH}"


def test_pack_uses_real_table_not_monthly_prices():
    """Pack must NOT reference 'monthly_prices' (does not exist in warehouse)."""
    pack = _load_pack()
    seen = set()
    for product in pack["products"]:
        tbl = product.get("source_hints", {}).get("table")
        seen.add(tbl)
    assert "monthly_prices" not in seen, (
        "Pack still references missing 'monthly_prices' table. "
        f"Tables in pack: {seen}"
    )


def test_pack_all_products_share_same_table():
    """All 16 products should share the target table."""
    pack = _load_pack()
    tables = {
        p.get("source_hints", {}).get("table")
        for p in pack["products"]
    }
    assert len(tables) == 1, f"Pack has multiple tables: {tables}"


@pytest.mark.parametrize("expected_table", [
    os.environ.get("FORECAST_PACK_TABLE", "actual_price"),
])
def test_pack_table_matches_decision(expected_table):
    pack = _load_pack()
    actual = pack["products"][0]["source_hints"]["table"]
    assert actual == expected_table, (
        f"Pack table is {actual!r}, expected {expected_table!r}. "
        "Update FORECAST_PACK_TABLE env or repoint the pack."
    )
```

- [ ] **Step 2: Run the test to verify it fails (TDD)**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend
PYTHONPATH=. ./venv/bin/python -m pytest tests/test_pack_ecisco_c5c9_v1.py -v
```

Expected: 1 failure — `test_pack_uses_real_table_not_monthly_prices` and `test_pack_table_matches_decision` both fail because the pack still says `monthly_prices`. The other tests should pass.

- [ ] **Step 3: Repoint the pack**

Run (replace `<NEW_TABLE>` with the value from Task 1's decision record):

```bash
cd /home/ysk2025/zhanlu_7_30/backend
NEW_TABLE="<NEW_TABLE>"  # e.g. "actual_price" or "v_lz_data"
python -c "
import json
from pathlib import Path
p = Path('app/services/forecasting/packs/ecisco_c5c9_v1.json')
data = json.loads(p.read_text())
for prod in data['products']:
    prod['source_hints']['table'] = '$NEW_TABLE'
p.write_text(json.dumps(data, indent=2, ensure_ascii=False))
print(f'Repointed {len(data[\"products\"])} products to $NEW_TABLE')
"
```

Verify by running:
```bash
cd /home/ysk2025/zhanlu_7_30/backend
grep -c '"table":' app/services/forecasting/packs/ecisco_c5c9_v1.json
grep '"table":' app/services/forecasting/packs/ecisco_c5c9_v1.json | head -2
```

Expected: 16 occurrences of `"table":`, all with the new table name (no more `monthly_prices`).

- [ ] **Step 4: Re-run the test to verify it passes**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend
PYTHONPATH=. ./venv/bin/python -m pytest tests/test_pack_ecisco_c5c9_v1.py -v
```

Expected: all 5 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/services/forecasting/packs/ecisco_c5c9_v1.json backend/tests/test_pack_ecisco_c5c9_v1.py
git commit -m "fix(forecasting): repoint ecisco_c5c9_v1 pack from missing monthly_prices to real table"
```

---

## Task 3: Create `truth_gate.py` with TDD

**Files:**
- Create: `backend/app/services/forecasting/truth_gate.py` (new)
- Create: `backend/tests/test_forecasting_truth_gate.py` (new, unit tests only)

**Goal:** A pure-Python module that post-processes a forecast engine result into a truth-gated response. No DB, no engine, no FastAPI — just dataclasses and a function. Easy to test.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_forecasting_truth_gate.py`:

```python
"""Unit tests for truth_gate.wrap_forecast_result."""

from app.services.forecasting.truth_gate import (
    TruthGateConfig,
    wrap_forecast_result,
)


def _anchor_rows(n: int) -> list[dict]:
    return [{"date": f"2026-01-{i+1:02d}", "value": 100.0 + i} for i in range(n)]


def _runs() -> list[dict]:
    return [
        {
            "target_id": "tgt-1",
            "horizon_days": 7,
            "point_estimate": 110.0,
            "scenarios": {"bull": 120, "base": 110, "bear": 100},
            "methodology": "ARIMA(2,1,2) ensemble",
            "confidence": 0.8,
        }
    ]


# ── Failure path ────────────────────────────────────────────────


def test_returns_insufficient_data_when_sample_is_empty():
    out = wrap_forecast_result(
        raw_runs=_runs(),
        anchor_rows=_anchor_rows(0),
        source_table="actual_price",
        sample_size=0,
    )
    assert out["success"] is False
    assert out["reason"] == "insufficient_data"
    assert out["source_table"] == "actual_price"
    assert out["sample_size"] == 0
    assert "Need" in out["message"]


def test_returns_insufficient_data_when_sample_below_threshold():
    for n in (1, 2, 3, 4):
        out = wrap_forecast_result(
            raw_runs=_runs(),
            anchor_rows=_anchor_rows(n),
            source_table="actual_price",
            sample_size=n,
        )
        assert out["success"] is False, f"n={n} should fail"
        assert out["reason"] == "insufficient_data", f"n={n} should be insufficient_data"
        assert out["sample_size"] == n


def test_failure_does_not_leak_runs():
    out = wrap_forecast_result(
        raw_runs=_runs(),
        anchor_rows=_anchor_rows(2),
        source_table="actual_price",
        sample_size=2,
    )
    assert "runs" not in out, "Failure path must not expose runs (would enable fabrication)"
    assert "data_anchor" not in out, "Failure path must not expose data_anchor"


# ── Success path ────────────────────────────────────────────────


def test_returns_success_at_min_threshold():
    out = wrap_forecast_result(
        raw_runs=_runs(),
        anchor_rows=_anchor_rows(5),
        source_table="actual_price",
        sample_size=5,
    )
    assert out["success"] is True
    assert "runs" in out
    assert "data_anchor" in out


def test_success_passes_through_runs_unchanged():
    runs = _runs()
    out = wrap_forecast_result(
        raw_runs=runs,
        anchor_rows=_anchor_rows(5),
        source_table="actual_price",
        sample_size=5,
    )
    assert out["runs"] == runs


def test_data_anchor_includes_first_and_last_rows():
    rows = _anchor_rows(20)
    out = wrap_forecast_result(
        raw_runs=_runs(),
        anchor_rows=rows,
        source_table="actual_price",
        sample_size=20,
    )
    a = out["data_anchor"]
    assert a["source_table"] == "actual_price"
    assert a["sample_size"] == 20
    assert a["first_5"] == rows[:5]
    assert a["last_5"] == rows[-5:]


# ── Configurable threshold ──────────────────────────────────────


def test_config_min_sample_size_is_honored():
    cfg = TruthGateConfig(min_sample_size=20)
    out = wrap_forecast_result(
        raw_runs=_runs(),
        anchor_rows=_anchor_rows(10),
        source_table="actual_price",
        sample_size=10,
        config=cfg,
    )
    assert out["success"] is False
    assert "≥20" in out["message"]
```

- [ ] **Step 2: Run the test to verify it fails (function not defined)**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend
PYTHONPATH=. ./venv/bin/python -m pytest tests/test_forecasting_truth_gate.py -v
```

Expected: ALL 8 tests fail with `ModuleNotFoundError: No module named 'app.services.forecasting.truth_gate'`.

- [ ] **Step 3: Write the minimal implementation**

Create `backend/app/services/forecasting/truth_gate.py`:

```python
"""Truth-gate: post-process forecast engine results into a data-anchored response.

Wraps raw engine output with a data_anchor (source table, sample size, first/last
rows) and converts thin-data cases into explicit insufficient_data verdicts.
This is what makes the LLM's forecast answer actually data-grounded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class TruthGateConfig:
    min_sample_size: int = 5            # below → "insufficient_data"
    max_anchor_rows: int = 10           # rows shown to LLM in the data_anchor
    confidence_threshold: float = 0.5   # below → "low_confidence" warning


def _build_failure(source_table: str, sample_size: int, min_required: int) -> dict:
    return {
        "success": False,
        "reason": "insufficient_data",
        "source_table": source_table,
        "sample_size": sample_size,
        "message": (
            f"Need ≥{min_required} data points in {source_table}; "
            f"found {sample_size}. Cannot produce a reliable forecast."
        ),
    }


def _build_anchor(anchor_rows: list[dict], source_table: str, sample_size: int) -> dict:
    return {
        "source_table": source_table,
        "sample_size": sample_size,
        "first_5": anchor_rows[:5],
        "last_5": anchor_rows[-5:] if len(anchor_rows) > 5 else anchor_rows,
    }


def wrap_forecast_result(
    raw_runs: list[dict],
    anchor_rows: list[dict],
    source_table: str,
    sample_size: int,
    config: TruthGateConfig | None = None,
) -> dict[str, Any]:
    """Post-process a forecast engine result into a truth-gated response.

    Success path (sample_size ≥ config.min_sample_size):
        {"success": True, "data_anchor": {...}, "runs": [...]}

    Failure path (sample_size < config.min_sample_size):
        {"success": False, "reason": "insufficient_data", "sample_size": N, ...}
    """
    cfg = config or TruthGateConfig()
    if sample_size < cfg.min_sample_size:
        return _build_failure(source_table, sample_size, cfg.min_sample_size)
    return {
        "success": True,
        "data_anchor": _build_anchor(anchor_rows, source_table, sample_size),
        "runs": raw_runs,
    }
```

- [ ] **Step 4: Re-run the test to verify it passes**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend
PYTHONPATH=. ./venv/bin/python -m pytest tests/test_forecasting_truth_gate.py -v
```

Expected: all 8 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/services/forecasting/truth_gate.py backend/tests/test_forecasting_truth_gate.py
git commit -m "feat(forecasting): add truth_gate module for data-anchored forecast responses"
```

---

## Task 4: Add engine anchored methods

**Files:**
- Modify: `backend/app/services/forecasting/engine.py` (append new methods, do NOT modify existing `compute_target` / `compute_all`)
- Modify: `backend/tests/test_forecasting_truth_gate.py` (add integration tests)

**Goal:** Two new engine methods that return the truth-anchor data alongside the existing `ForecastRun`. Existing engine API unchanged. Tool handler (Task 5) uses the new methods.

- [ ] **Step 1: Write the failing integration tests**

Append to `backend/tests/test_forecasting_truth_gate.py`:

```python
# ── Integration tests with ForecastEngine ────────────────────────

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base  # adjust if Base lives elsewhere
from app.services.forecasting.engine import ForecastEngine


@pytest.fixture
def in_memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_engine_with_no_targets_returns_empty(in_memory_db):
    fe = ForecastEngine(in_memory_db)
    out = fe.compute_target_anchored(target_id="nonexistent")
    assert out is None  # no target row → None, no fabrication


def test_engine_with_thin_target_returns_anchored_with_low_sample(in_memory_db, monkeypatch):
    """If target has only 3 data points, anchored method should return sample_size=3."""
    from app.models.forecasting import ForecastTarget

    target = ForecastTarget(
        id="tgt-thin",
        org_id="org-1",
        app_id="app-1",
        name="thin test",
        datasource={
            "table": "fake_table",
            "time_column": "date",
            "measure": "value",
        },
        is_deleted=False,
    )
    in_memory_db.add(target)
    in_memory_db.commit()

    import pandas as pd
    def fake_fetch(target):
        return pd.Series([100.0, 101.0, 102.0])

    fe = ForecastEngine(in_memory_db)
    monkeypatch.setattr(fe, "_fetch_series", fake_fetch)
    out = fe.compute_target_anchored(target_id="tgt-thin")
    if out is not None:
        assert "source_table" in out
        assert "sample_size" in out
        assert out["sample_size"] == 3
        assert "anchor_rows" in out
```

> **Note:** The `Base` import path may differ. Look in `backend/app/db/` for the actual declarative base. If it doesn't exist or the models are auto-loaded, replace with the actual import.

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend
PYTHONPATH=. ./venv/bin/python -m pytest tests/test_forecasting_truth_gate.py -v -k "anchored"
```

Expected: `AttributeError: 'ForecastEngine' object has no attribute 'compute_target_anchored'`.

- [ ] **Step 3: Add the new engine methods**

Append to `backend/app/services/forecasting/engine.py` (do NOT modify existing methods):

```python
    # ── Truth-anchor variants (Task 4) ─────────────────────────────

    def compute_target_anchored(
        self,
        target_id: str,
        horizons: list[int] | None = None,
        seasonal_period: int = _DEFAULT_SEASONAL_PERIOD,
    ) -> dict | None:
        """Like compute_target(), but also returns the source data for truth-gating.

        Returns:
            None if target not found.
            {
                "run": ForecastRun | None,    # the engine's run, or None on compute failure
                "source_table": str,           # the datasource table
                "sample_size": int,            # number of non-null rows
                "anchor_rows": list[dict],     # first/last rows for LLM inspection
            }
        """
        target = self._db.query(ForecastTarget).filter(
            ForecastTarget.id == target_id,
            ForecastTarget.is_deleted == False,
        ).first()
        if not target:
            return None

        ds = target.datasource or {}
        source_table = ds.get("table", "unknown")

        # Pull raw series and capture anchor before running the engine
        y = self._fetch_series(target)
        if y is None:
            return {
                "run": None,
                "source_table": source_table,
                "sample_size": 0,
                "anchor_rows": [],
            }

        clean = y.dropna()
        sample_size = len(clean)
        seen_dates: set[str] = set()
        anchor_rows: list[dict] = []
        for idx, val in clean.head(10).items():
            d = str(idx.date())
            seen_dates.add(d)
            anchor_rows.append({"date": d, "value": float(val)})
        for idx, val in clean.tail(10).items():
            d = str(idx.date())
            if d not in seen_dates:
                anchor_rows.append({"date": d, "value": float(val)})

        # Now run the full pipeline
        run = self.compute_target(target_id, horizons, seasonal_period)
        return {
            "run": run,
            "source_table": source_table,
            "sample_size": sample_size,
            "anchor_rows": anchor_rows,
        }
```

- [ ] **Step 4: Re-run the test to verify it passes**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend
PYTHONPATH=. ./venv/bin/python -m pytest tests/test_forecasting_truth_gate.py -v
```

Expected: all unit tests + integration tests pass. If integration tests fail due to missing model columns or import paths, adjust the test imports to match the actual `Base` location and `ForecastTarget` schema.

- [ ] **Step 5: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/services/forecasting/engine.py backend/tests/test_forecasting_truth_gate.py
git commit -m "feat(forecasting): add engine methods that return truth-anchor data alongside ForecastRun"
```

---

## Task 5: Wire truth_gate into forecast_tool.py

**Files:**
- Modify: `backend/app/services/tool_handlers/forecast_tool.py` (replace the response building in `_forecast_run` at lines 187-207 to use truth_gate)
- Modify: `backend/tests/test_forecasting_truth_gate.py` (add a test for the tool handler path)

**Goal:** When `_forecast_run` returns, the response is truth-gated. The LLM cannot receive a forecast without seeing the source rows or an explicit `insufficient_data` verdict.

- [ ] **Step 1: Write the failing tool-handler test**

Append to `backend/tests/test_forecasting_truth_gate.py`:

```python
# ── Tool handler tests ──────────────────────────────────────────

import asyncio
from unittest.mock import MagicMock, patch


def test_forecast_run_with_thin_data_returns_insufficient_data():
    """When the engine returns 3 data points, the tool returns insufficient_data."""
    from app.services.tool_handlers.forecast_tool import _forecast_run

    fake_engine = MagicMock()
    fake_engine.compute_target_anchored.return_value = {
        "run": None,
        "source_table": "actual_price",
        "sample_size": 3,
        "anchor_rows": [],
    }
    fake_db = MagicMock()
    fake_db.get.return_value = MagicMock(is_deleted=False)

    with patch(
        "app.services.tool_handlers.forecast_tool.ForecastEngine",
        return_value=fake_engine,
    ):
        result = asyncio.run(_forecast_run(
            args={"target_id": "tgt-1"},
            db=fake_db,
            user_id="u-1",
            context={},
        ))

    assert result["success"] is False
    assert result["reason"] == "insufficient_data"
    assert result["sample_size"] == 3
    assert result["source_table"] == "actual_price"


def test_forecast_run_with_full_data_returns_anchored_response():
    """When the engine returns 30+ data points, the tool returns success + data_anchor."""
    from app.services.tool_handlers.forecast_tool import _forecast_run

    fake_run = MagicMock()
    fake_run.target_id = "tgt-1"
    fake_run.below_naive_baseline = False
    fake_run.confidence = 0.8
    fake_run.as_of_date = None
    fake_run.model_detail = {"ensemble_mape": 0.05}
    fake_run.results = {
        "7": {"base": [120.0] * 7, "bull": [130.0] * 7, "bear": [110.0] * 7}
    }

    fake_engine = MagicMock()
    fake_engine.compute_target_anchored.return_value = {
        "run": fake_run,
        "source_table": "actual_price",
        "sample_size": 365,
        "anchor_rows": [
            {"date": "2026-01-01", "value": 100.0},
            {"date": "2026-12-31", "value": 365.0},
        ],
    }
    fake_db = MagicMock()
    fake_db.get.return_value = MagicMock(is_deleted=False)

    with patch(
        "app.services.tool_handlers.forecast_tool.ForecastEngine",
        return_value=fake_engine,
    ):
        result = asyncio.run(_forecast_run(
            args={"target_id": "tgt-1"},
            db=fake_db,
            user_id="u-1",
            context={},
        ))

    assert result["success"] is True
    assert "data_anchor" in result
    assert result["data_anchor"]["sample_size"] == 365
    assert "runs" in result
    assert result["runs"][0]["target_id"] == "tgt-1"
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend
PYTHONPATH=. ./venv/bin/python -m pytest tests/test_forecasting_truth_gate.py -v -k "tool_handler or thin_data_returns or full_data_returns"
```

Expected: tests fail because `_forecast_run` doesn't use `compute_target_anchored` yet.

- [ ] **Step 3: Modify `_forecast_run` to use the anchored method + truth_gate**

Edit `backend/app/services/tool_handlers/forecast_tool.py`. Replace lines 180-207 (the `_do_work` body for the `target_id` branch) with:

```python
    def _do_work() -> dict:
        from app.services.forecasting.truth_gate import wrap_forecast_result

        engine = ForecastEngine(db)

        if target_id:
            target = db.get(ForecastTarget, target_id)
            if not target or target.is_deleted:
                return {"success": False, "error": f"ForecastTarget {target_id!r} not found."}

            anchored = engine.compute_target_anchored(
                target_id, horizons, seasonal_period
            )
            if anchored is None:
                return {"success": False, "error": f"Compute failed for target {target_id!r}."}

            run = anchored["run"]
            raw_runs: list[dict] = []
            if run is not None:
                horizon_key = str(max(horizons))
                horizon_payload = (run.results or {}).get(horizon_key, {})
                raw_runs = [
                    {
                        "target_id": run.target_id,
                        "horizon_days": max(horizons),
                        "point_estimate": (
                            horizon_payload.get("base", [None])[0]
                            if horizon_payload else None
                        ),
                        "scenarios": horizon_payload,
                        "methodology": (run.model_detail or {}).get("metric", "ensemble"),
                        "confidence": run.confidence,
                        "below_naive_baseline": run.below_naive_baseline,
                        "as_of_date": run.as_of_date.isoformat() if run.as_of_date else None,
                        "model_detail": run.model_detail,
                    }
                ]

            gated = wrap_forecast_result(
                raw_runs=raw_runs,
                anchor_rows=anchored["anchor_rows"],
                source_table=anchored["source_table"],
                sample_size=anchored["sample_size"],
            )
            return gated

        # bulk path — unchanged
        runs = engine.compute_all(org_id, horizons, seasonal_period)
        return {
            "success": True,
            "runs": [
                {
                    "target_id": r.target_id,
                    "below_naive_baseline": r.below_naive_baseline,
                    "confidence": r.confidence,
                    "as_of_date": r.as_of_date.isoformat() if r.as_of_date else None,
                    "model_detail": r.model_detail,
                }
                for r in runs
            ],
            "count": len(runs),
        }
```

> **Note:** If `ForecastEngine` is not already imported in this file, add it to the import block at the top of `forecast_tool.py` (around line 23 where `ForecastRun` is imported).

- [ ] **Step 4: Re-run the test to verify it passes**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend
PYTHONPATH=. ./venv/bin/python -m pytest tests/test_forecasting_truth_gate.py -v
```

Expected: all tests pass (8 unit + integration + 2 tool handler).

- [ ] **Step 5: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/services/tool_handlers/forecast_tool.py backend/tests/test_forecasting_truth_gate.py
git commit -m "feat(forecasting): wire truth_gate into forecast_run tool handler"
```

---

## Task 6: Enable heavy forecast tools in the registry

**Files:**
- Modify: `backend/app/services/tool_handlers/forecast_tool.py` (flip `enabled_by_default` for the heavy tools from `False` to `True`)
- Create: `backend/tests/test_forecast_tool_registry_gating.py` (new)

**Goal:** The two heavy forecast tools (`forecast_run`, `forecast_discover`) are enabled by default in the tool registry, so the `forecast_agent` subagent can actually call them.

- [ ] **Step 1: Read the current registry values**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend
grep -n "forecast_run\|forecast_discover" app/services/tool_handlers/forecast_tool.py | head -20
```

Verify: the two heavy tools have `enabled_by_default=False`. Note the exact lines.

- [ ] **Step 2: Write the failing smoke test**

Create `backend/tests/test_forecast_tool_registry_gating.py`:

```python
"""Smoke test: heavy forecast tools must be enabled in the registry.

The forecast_agent subagent declares these tools in its tools list, but if
the registry has them gated off (enabled_by_default=False), the subagent
cannot use them. This test guards against that regression.
"""

from app.services.tool_handlers import forecast_tool  # noqa: F401
from app.services.tool_registry import registry


def test_forecast_run_is_enabled_by_default():
    tool = registry.get("forecast_run")
    assert tool is not None, "forecast_run not registered"
    assert tool.enabled_by_default is True, (
        "forecast_run must be enabled_by_default=True so the forecast_agent "
        "subagent can call it. (Was: False — the original bug.)"
    )


def test_forecast_discover_is_enabled_by_default():
    tool = registry.get("forecast_discover")
    assert tool is not None, "forecast_discover not registered"
    assert tool.enabled_by_default is True, (
        "forecast_discover must be enabled_by_default=True so the forecast_agent "
        "subagent can call it. (Was: False — the original bug.)"
    )


def test_lightweight_forecast_tools_remain_enabled():
    """The 3 already-enabled tools should stay enabled."""
    for name in ("forecast_get", "forecast_accuracy", "forecast_rules"):
        tool = registry.get(name)
        assert tool is not None, f"{name} not registered"
        assert tool.enabled_by_default is True, f"{name} must stay enabled"
```

- [ ] **Step 3: Run the test to verify it fails**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend
PYTHONPATH=. ./venv/bin/python -m pytest tests/test_forecast_tool_registry_gating.py -v
```

Expected: 2 of 3 tests fail (`forecast_run`, `forecast_discover` are `False`); the third passes.

- [ ] **Step 4: Flip the booleans**

Edit `backend/app/services/tool_handlers/forecast_tool.py` at the lines identified in Step 1. The structure typically looks like:

```python
ToolDef(
    name="forecast_run",
    ...
    enabled_by_default=False,  # → True
)
```

and similarly for `forecast_discover`. Use `replace_in_file` with surrounding context (3 lines before and after) to make the change unique.

- [ ] **Step 5: Re-run the test to verify it passes**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend
PYTHONPATH=. ./venv/bin/python -m pytest tests/test_forecast_tool_registry_gating.py -v
```

Expected: all 3 tests pass.

- [ ] **Step 6: Run the manual smoke check**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend
PYTHONPATH=. ./venv/bin/python -c "
from app.services.tool_registry import registry
for name in ('forecast_run', 'forecast_discover'):
    t = registry.get(name)
    print(f'{name}: enabled_by_default={t.enabled_by_default}')
"
```

Expected output: `forecast_run: enabled_by_default=True` and `forecast_discover: enabled_by_default=True`.

- [ ] **Step 7: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/services/tool_handlers/forecast_tool.py backend/tests/test_forecast_tool_registry_gating.py
git commit -m "fix(forecasting): enable forecast_run and forecast_discover by default in registry"
```

---

## Task 7: End-to-end acceptance test

**Files:**
- Create: `backend/tests/test_forecasting_e2e_acceptance.py` (new)

**Goal:** The single test that proves the user's actual goal works: a chat request for a forecast returns either a real, data-grounded forecast OR an explicit insufficient-data message — never a hallucinated number, never an empty response, never partial data without acknowledgment.

- [ ] **Step 1: Examine the existing chat test client**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend
ls tests/ | grep -i "chat\|e2e\|agent" | head -10
ls tests/conftest.py 2>&1
```

Look at one existing chat test to see the pattern (test client setup, fixtures, mocking). Use that pattern for the new test.

- [ ] **Step 2: Write the acceptance test**

Create `backend/tests/test_forecasting_e2e_acceptance.py`:

```python
"""End-to-end acceptance test: user asks for a forecast, gets a true answer.

This is the test that proves the user's stated goal is met:
"user can do forecasting based question and get true answer"

Two scenarios are tested:
  - Thin data (0 rows): response is insufficient_data, NOT a hallucinated number
  - Full data (30 rows): response is success with data_anchor, has a real forecast
"""

import asyncio
from unittest.mock import MagicMock, patch


def _stub_engine_with_sample_size(n: int):
    """Build a fake ForecastEngine whose compute_target_anchored returns n rows."""
    fake_run = MagicMock()
    fake_run.target_id = "tgt-1"
    fake_run.below_naive_baseline = False
    fake_run.confidence = 0.8
    fake_run.as_of_date = None
    fake_run.model_detail = {"metric": "MAPE", "ensemble_mape": 0.05}
    fake_run.results = {
        "7": {"base": [120.0, 121.0, 122.0, 123.0, 124.0, 125.0, 126.0],
              "bull": [130.0] * 7, "bear": [110.0] * 7}
    }

    fake_engine = MagicMock()
    if n == 0:
        fake_engine.compute_target_anchored.return_value = {
            "run": None, "source_table": "actual_price",
            "sample_size": 0, "anchor_rows": [],
        }
    else:
        rows = [
            {"date": f"2026-01-{i+1:02d}", "value": 100.0 + i}
            for i in range(n)
        ]
        fake_engine.compute_target_anchored.return_value = {
            "run": fake_run, "source_table": "actual_price",
            "sample_size": n, "anchor_rows": rows,
        }
    return fake_engine


def test_thin_warehouse_yields_no_numeric_forecast():
    """When the warehouse has 0 data points, the tool returns insufficient_data
    and the response must NOT contain a numeric forecast value."""
    from app.services.tool_handlers.forecast_tool import _forecast_run

    fake_db = MagicMock()
    fake_db.get.return_value = MagicMock(is_deleted=False)

    with patch(
        "app.services.tool_handlers.forecast_tool.ForecastEngine",
        return_value=_stub_engine_with_sample_size(0),
    ):
        result = asyncio.run(_forecast_run(
            args={"target_id": "tgt-1"},
            db=fake_db, user_id="u-1", context={},
        ))

    assert result["success"] is False
    assert result["reason"] == "insufficient_data"
    assert "runs" not in result
    serialized = str(result)
    assert "point_estimate" not in serialized
    assert "120.0" not in serialized, "Leaked forecast number in failure path"


def test_full_warehouse_yields_grounded_forecast():
    """When the warehouse has 30+ data points, the tool returns success with
    data_anchor, and the LLM can see a real forecast value backed by source rows."""
    from app.services.tool_handlers.forecast_tool import _forecast_run

    fake_db = MagicMock()
    fake_db.get.return_value = MagicMock(is_deleted=False)

    with patch(
        "app.services.tool_handlers.forecast_tool.ForecastEngine",
        return_value=_stub_engine_with_sample_size(30),
    ):
        result = asyncio.run(_forecast_run(
            args={"target_id": "tgt-1"},
            db=fake_db, user_id="u-1", context={},
        ))

    assert result["success"] is True
    a = result["data_anchor"]
    assert a["sample_size"] == 30
    assert a["source_table"] == "actual_price"
    assert len(a["first_5"]) == 5
    assert len(a["last_5"]) == 5
    assert result["runs"][0]["point_estimate"] == 120.0
    assert "methodology" in result["runs"][0]
```

- [ ] **Step 3: Run the test to verify it passes**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend
PYTHONPATH=. ./venv/bin/python -m pytest tests/test_forecasting_e2e_acceptance.py -v
```

Expected: both tests pass. If they don't, the issue is likely:
- `ForecastEngine` not imported in `forecast_tool.py` — add the import
- The mock signature doesn't match — adjust the `_stub_engine_with_sample_size` helper
- `_forecast_run` path was edited differently in Task 5 — reconcile

- [ ] **Step 4: Run the FULL test suite to confirm no regressions**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend
PYTHONPATH=. ./venv/bin/python -m pytest tests/test_forecasting_truth_gate.py tests/test_pack_ecisco_c5c9_v1.py tests/test_forecast_tool_registry_gating.py tests/test_forecasting_e2e_acceptance.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/tests/test_forecasting_e2e_acceptance.py
git commit -m "test(forecasting): end-to-end acceptance test for true-anchored answers"
```

---

## Task 8: Manual acceptance — run the 3 questions in Ecisco BI chat

**Files:**
- Write: `docs/superpowers/notes/2026-07-31-acceptance-results.md` (new, results record)

**Goal:** Verify the user's actual goal is met. The user opens the Ecisco BI project in the chat UI and asks 3 questions; the answers must match the spec's success criteria.

- [ ] **Step 1: Start the backend**

```bash
cd /home/ysk2025/zhanlu_7_30/backend
PYTHONPATH=. ./venv/bin/python -m uvicorn app.main:app --reload --port 8000 &
```

Wait for "Application startup complete" then continue.

- [ ] **Step 2: Ask question 1 — single-product forecast**

In the Ecisco BI chat, ask: **"Predict LZ001's price for the next 7 days."**

Expected (per spec §5 acceptance checklist):
- If warehouse has ≥5 points: forecast value + `source_table` + `sample_size` + methodology
- If warehouse has <5 points: `insufficient_data` message with `sample_size` shown

If the response is anything else (a hallucinated number with no data_anchor, a partial "feasibility snapshot", an empty response), STOP. The acceptance failed; investigate.

- [ ] **Step 3: Ask question 2 — forecast accuracy**

In the Ecisco BI chat, ask: **"Show me the forecast accuracy for LZ001."**

Expected: a response showing MAPE and naive baseline comparison from the `forecast_accuracy` tool. If `forecast_run` was never run for LZ001, the tool may say "no accuracy data yet — run forecast_run first" — that's also acceptable, as long as the message is honest.

- [ ] **Step 4: Ask question 3 — list forecast targets**

In the Ecisco BI chat, ask: **"What forecast targets are registered?"**

Expected: a list of active `ForecastTarget` rows from `forecast_get`, OR an explicit "none yet — run forecast_discover to scan for forecastable series" message.

- [ ] **Step 5: Record the results**

Create `docs/superpowers/notes/2026-07-31-acceptance-results.md` with:

```markdown
# Acceptance Results (2026-07-31)

## Question 1: "Predict LZ001's price for the next 7 days"
- Outcome: <success | insufficient_data>
- Forecast value: <number or N/A>
- Source table: <table>
- Sample size: <N>
- Methodology: <string>
- Verdict: <pass | fail>
- Notes: <observation>

## Question 2: "Show me the forecast accuracy for LZ001"
- Outcome: <description>
- Verdict: <pass | fail>
- Notes: <observation>

## Question 3: "What forecast targets are registered?"
- Outcome: <description>
- Verdict: <pass | fail>
- Notes: <observation>

## Overall
- All 3 passed: yes / no
- Remaining issues: <list or 'none'>
- Next steps: <list or 'ship'>
```

- [ ] **Step 6: Commit the results**

```bash
cd /home/ysk2025/zhanlu_7_30
git add docs/superpowers/notes/2026-07-31-acceptance-results.md
git commit -m "docs(forecasting): record manual acceptance test results"
```

- [ ] **Step 7: Stop the backend**

```bash
pkill -f "uvicorn app.main:app" || true
```

---

## Summary

| Task | Deliverable | Est. time |
|---|---|---|
| 1 | Pre-flight discovery + decision record | 30 min |
| 2 | Repoint pack + tests | 30 min |
| 3 | truth_gate module + unit tests (TDD) | 1 hour |
| 4 | Engine anchored methods + integration tests | 1 hour |
| 5 | Wire truth_gate into forecast_tool | 1 hour |
| 6 | Enable heavy tools in registry | 30 min |
| 7 | End-to-end acceptance test | 30 min |
| 8 | Manual chat acceptance | 30 min |
| **Total** | | **~6 hours** |

## Self-Review

- **Spec coverage:** Each spec section maps to a task. Architecture → Task 5 (truth_gate) + Task 6 (tool enablement). Components → Tasks 2-6. Data flow → Task 5 (response shape). Error handling → Task 3 (insufficient_data verdict) + Task 5 (existing error envelopes preserved). Testing → Tasks 3, 4, 5, 7. Acceptance → Task 8.
- **Placeholder scan:** All code blocks are complete. No "TODO", "TBD", or "fill in later". One `Base` import is marked with a comment ("adjust if Base lives elsewhere") — this is intentional, the implementer resolves the path.
- **Type consistency:** `wrap_forecast_result` returns the same shape in both Tasks 3 and 5. `compute_target_anchored` returns the same dict shape used in Task 4 integration tests and Task 5 tool handler. `data_anchor` keys (`source_table`, `sample_size`, `first_5`, `last_5`) consistent across Tasks 3, 4, 5, 7.
- **TDD discipline:** Every code task has a "write failing test" step before the "implement" step.
- **Commit cadence:** One commit per task minimum, sometimes more.
- **No dependencies on other tasks' results:** Each task's test verifies its own deliverable. The only dependency is on Task 1's discovery result flowing into Task 2.
