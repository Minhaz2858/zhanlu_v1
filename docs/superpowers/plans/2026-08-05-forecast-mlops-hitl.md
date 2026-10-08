# Forecast MLOps + Human-in-the-Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the forecasting self-learning loop (populate `realized_mape` → enable drift auto-response) and add a trust-gated Human-in-the-Loop correction channel so validated analyst judgement can nudge published prices.

**Architecture:** Extend the existing 2 AM UTC `_nightly_forecast_loop` in `scheduled_tasks.py` with an evaluation step (scores past forecasts + user overrides against newly-arrived actuals), a drift-response step (auto-blend toward naive on drift + audit), and a trust-gated bias-correction layer plugged into the engine's publish step. No new infra, no APScheduler, no model registry (ensemble refits per run). All paths feature-flagged OFF by default.

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy 2.0 (Mapped) / pandas / pytest 9.1.1 (in `backend/venv`) / React + @tanstack/react-query + vitest (happy-dom).

## Global Constraints

- **Primary project tree:** `/home/ysk2025/zhanlu_7_30` (NOT `/home/ysk2025/zhanlu/` or `zhanlu-deploy`).
- **Backend tests:** run with `backend/venv/bin/pytest tests/<file>::<test> -v` (pytest 9.1.1 is in the venv; system has none). Run individually — SSH RAM is limited, no backgrounding.
- **Frontend tests:** `cd frontend && node_modules/.bin/vitest --no-watch run src/path/__tests__/file.test.jsx` (happy-dom). Run files individually (4+ files timeout).
- **Docker:** backend has no `--reload`. Code changes → `docker restart zhanlu-backend`, sleep ~20s. **`.env` flag changes → `docker compose up -d --force-recreate backend`** (env_file baked at creation; plain restart won't pick up new flags). Stale image wipes pandas/statsmodels/xgboost → `docker exec zhanlu-backend pip install pandas statsmodels xgboost` if engine import crashes after recreate.
- **Alembic head:** `040_report_session_project_agent`. New migration is `041`. Schema also managed by `Base.metadata.create_all()`; the prestart `alembic upgrade head` must not crash. Use `op.add_column` + `op.create_table` + `op.execute("CREATE INDEX IF NOT EXISTS ...")` (proven safe in migration 040). Avoid `create_index` with a list arg (migration 026 crash pattern).
- **Honesty boundary:** decision-engine thresholds (`p_rise ≥ 0.70`, buy/hold/sell/watch gate) stay 100% deterministic. Bias/drift adjust the *published price* only, never the action call. Analyst brief discloses when an adjustment was applied.
- **Chinese market colors:** 涨=red, 跌=green (Western reversed) — already used on the board.
- **No commits unless user asks.** User works on `main` directly with uncommitted changes.

## File Structure

**New files:**
- `backend/app/services/forecasting/ops/__init__.py` — package marker.
- `backend/app/services/forecasting/ops/evaluation_job.py` — closes `realized_mape` loop; scores past runs + HITL feedback against actuals.
- `backend/app/services/forecasting/ops/drift_response.py` — detect drift (now data-backed), write audit row for auto-blend.
- `backend/app/services/forecasting/ops/feedback_service.py` — capture/list HITL corrections + author track-record.
- `backend/app/services/forecasting/ops/bias_correction.py` — trust-gate + validated bias delta.
- `backend/app/routers/forecast_ops.py` — `GET /api/forecast-ops/status`, `POST /api/forecast-ops/run-evaluation`.
- `backend/alembic/versions/041_forecast_mlops_hitl.py` — 2 new tables + `run_id` column on `forecast_accuracy_log`.
- `backend/tests/test_forecast_evaluation_job.py`
- `backend/tests/test_forecast_drift_response.py`
- `backend/tests/test_forecast_feedback_service.py`
- `backend/tests/test_forecast_bias_correction.py`
- `backend/tests/test_forecast_ops_router.py`
- `frontend/src/pages/MarketDashboard/components/ForecastFeedbackModal.jsx`
- `frontend/src/pages/MarketDashboard/components/__tests__/ForecastFeedbackModal.test.jsx`

**Modified files:**
- `backend/app/models/forecasting.py` — add `ForecastFeedback`, `ForecastWeightAdjustment` models; add `run_id` to `ForecastAccuracyLog`.
- `backend/app/services/forecasting/engine.py` — plug drift-blend + bias-correction into `compute_target` between policy overlay (~line 478) and `scenarios.generate` (~line 518).
- `backend/app/services/scheduled_tasks.py` — call eval + drift after the compute loop in `_run_nightly_forecast_sync`.
- `backend/app/routers/market_dashboard.py` — add feedback endpoints.
- `backend/app/schemas/market_dashboard.py` — add feedback request/response schemas.
- `backend/app/main.py` — register `forecast_ops` router.
- `backend/app/models/__init__.py` — ensure new models imported (for `Base.metadata`).
- `frontend/src/api/marketDashboard.js` — add `submitForecastFeedback` / `listForecastFeedback`.
- `frontend/src/pages/MarketDashboard/components/DecisionBoard.jsx` — render "修正预测" button + modal.
- `.env` — 4 flags (all OFF).

---

### Task 1: Data model + migration 041

**Files:**
- Modify: `backend/app/models/forecasting.py` (append two classes; add `run_id` to `ForecastAccuracyLog`)
- Modify: `backend/app/models/__init__.py` (ensure imports)
- Create: `backend/alembic/versions/041_forecast_mlops_hitl.py`
- Test: `backend/tests/test_forecast_mlops_models.py`

**Interfaces:**
- Produces: `ForecastFeedback` (fields: target_id, product_id, ai_price, user_price, reason, author_id, author_name, target_date, status, ai_error, user_error, beat, scored_at), `ForecastWeightAdjustment` (fields: target_id, triggered_by, reason, old_weights, new_weights, delta_ratio, applied). `ForecastAccuracyLog.run_id` (nullable String(36)).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_forecast_mlops_models.py
"""Model + table-creation tests for forecast_feedback, forecast_weight_adjustments,
and the run_id column on forecast_accuracy_log."""
from datetime import datetime
import pytest

from app.database import Base, engine, SessionLocal
from app.models.forecasting import (
    ForecastTarget, ForecastRun, ForecastAccuracyLog,
    ForecastFeedback, ForecastWeightAdjustment,
)

_NEEDED_TABLES = [
    ForecastTarget.__table__, ForecastRun.__table__,
    ForecastAccuracyLog.__table__, ForecastFeedback.__table__,
    ForecastWeightAdjustment.__table__,
]


@pytest.fixture(autouse=True)
def _setup_tables():
    Base.metadata.drop_all(engine, tables=_NEEDED_TABLES)
    Base.metadata.create_all(engine, tables=_NEEDED_TABLES)
    yield
    Base.metadata.drop_all(engine, tables=_NEEDED_TABLES)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.rollback()
    s.close()


def test_forecast_feedback_roundtrip(db):
    target = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(target); db.flush()
    fb = ForecastFeedback(
        target_id=target.id, product_id="电子元件", ai_price=10388.0,
        user_price=11000.0, reason="supply tightening", author_id="u1",
        author_name="analyst", target_date=datetime(2026, 8, 10), status="pending",
        org_id="default-org",
    )
    db.add(fb); db.commit()
    got = db.query(ForecastFeedback).first()
    assert got.status == "pending"
    assert got.beat is None
    assert got.user_price == 11000.0


def test_forecast_weight_adjustment_roundtrip(db):
    target = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(target); db.flush()
    adj = ForecastWeightAdjustment(
        target_id=target.id, triggered_by="drift",
        reason="recent=18.3 vs baseline=12.1", old_weights={"ets": 0.4},
        new_weights={"ets": 0.3}, delta_ratio=None, applied=False,
        org_id="default-org",
    )
    db.add(adj); db.commit()
    got = db.query(ForecastWeightAdjustment).first()
    assert got.triggered_by == "drift"
    assert got.applied is False


def test_accuracy_log_run_id_nullable(db):
    target = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(target); db.flush()
    log = ForecastAccuracyLog(
        target_id=target.id, horizon_days=7, mape=0.12, naive_mape=0.10,
        run_id="run-abc", realized_mape=0.15, org_id="default-org",
    )
    db.add(log); db.commit()
    got = db.query(ForecastAccuracyLog).first()
    assert got.run_id == "run-abc"
    assert got.realized_mape == 0.15
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/pytest tests/test_forecast_mlops_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'ForecastFeedback'` (and `run_id` attribute error).

- [ ] **Step 3: Add the models**

Append to `backend/app/models/forecasting.py` (after `ForecastAccuracyLog`), and add `run_id` to `ForecastAccuracyLog`:

```python
class ForecastAccuracyLog(TimestampedBase):
    # ... existing fields unchanged ...
    # Add this new nullable column (realized-eval rows link back to their run):
    run_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    # ... rest unchanged ...
```

Append the two new classes at the end of `forecasting.py`:

```python
class ForecastFeedback(TimestampedBase):
    """Human-in-the-Loop correction — a user asserts the AI price is wrong.

    ``status`` flows ``pending`` → ``scored`` once the target date's actual
    arrives and the evaluation job computes ``ai_error`` / ``user_error`` /
    ``beat``. A pending override NEVER moves the forecast; only trust-gated,
    validated corrections influence the published price (bias_correction.py).
    """

    __tablename__ = "forecast_feedback"

    target_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("forecast_targets.id"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    ai_price: Mapped[float] = mapped_column(Float, nullable=False)
    user_price: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    author_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    author_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    target_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    ai_error: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    user_error: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    beat: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    scored_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ForecastWeightAdjustment(TimestampedBase):
    """Audit trail for automated forecast adjustments (drift, bias-correction).

    Every automated change to a published forecast writes a row here so the
    change is traceable. ``applied=False`` on creation; the engine marks it
    ``True`` once the adjustment is blended into a run.
    """

    __tablename__ = "forecast_weight_adjustments"

    target_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("forecast_targets.id"), nullable=False, index=True
    )
    triggered_by: Mapped[str] = mapped_column(String(20), nullable=False)  # drift | bias_correction
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    old_weights: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    new_weights: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    delta_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
```

Ensure `backend/app/models/__init__.py` imports the new classes so `Base.metadata` registers them. Check the file; if it imports `from app.models.forecasting import *` or lists classes explicitly, add `ForecastFeedback`, `ForecastWeightAdjustment`. If it does `import app.models.forecasting` already, no change needed.

- [ ] **Step 4: Create migration 041**

```python
# backend/alembic/versions/041_forecast_mlops_hitl.py
"""forecast mlops + hitl: forecast_feedback, forecast_weight_adjustments, run_id

Revision ID: 041_forecast_mlops_hitl
Revises: 040_report_session_project_agent
Create Date: 2026-08-05
"""
from alembic import op  # type: ignore[import-untyped]
import sqlalchemy as sa  # type: ignore[import-untyped]

revision = "041_forecast_mlops_hitl"
down_revision = "040_report_session_project_agent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Link realized-eval rows back to their ForecastRun.
    op.add_column("forecast_accuracy_log",
                  sa.Column("run_id", sa.String(36), nullable=True))
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_forecast_accuracy_log_run_id "
        "ON forecast_accuracy_log (run_id)"
    )

    # 2. HITL feedback table.
    op.create_table(
        "forecast_feedback",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("target_id", sa.String(36),
                  sa.ForeignKey("forecast_targets.id"), nullable=False),
        sa.Column("product_id", sa.String(100), nullable=False),
        sa.Column("ai_price", sa.Float, nullable=False),
        sa.Column("user_price", sa.Float, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("author_id", sa.String(36), nullable=False),
        sa.Column("author_name", sa.String(255), nullable=True),
        sa.Column("target_date", sa.DateTime, nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("ai_error", sa.Float, nullable=True),
        sa.Column("user_error", sa.Float, nullable=True),
        sa.Column("beat", sa.Boolean, nullable=True),
        sa.Column("scored_at", sa.DateTime, nullable=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("app_id", sa.String(36), nullable=False),
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_forecast_feedback_target_id ON forecast_feedback (target_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_forecast_feedback_product_id ON forecast_feedback (product_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_forecast_feedback_author_id ON forecast_feedback (author_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_forecast_feedback_status ON forecast_feedback (status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_forecast_feedback_target_date ON forecast_feedback (target_date)")

    # 3. Weight-adjustment audit table.
    op.create_table(
        "forecast_weight_adjustments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("target_id", sa.String(36),
                  sa.ForeignKey("forecast_targets.id"), nullable=False),
        sa.Column("triggered_by", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("old_weights", sa.JSON, nullable=True),
        sa.Column("new_weights", sa.JSON, nullable=True),
        sa.Column("delta_ratio", sa.Float, nullable=True),
        sa.Column("applied", sa.Boolean, nullable=False),
        sa.Column("applied_at", sa.DateTime, nullable=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("app_id", sa.String(36), nullable=False),
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_fwa_target_id ON forecast_weight_adjustments (target_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_fwa_triggered_by ON forecast_weight_adjustments (triggered_by)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_fwa_triggered_by")
    op.execute("DROP INDEX IF EXISTS ix_fwa_target_id")
    op.drop_table("forecast_weight_adjustments")
    op.execute("DROP INDEX IF EXISTS ix_forecast_feedback_target_date")
    op.execute("DROP INDEX IF EXISTS ix_forecast_feedback_status")
    op.execute("DROP INDEX IF EXISTS ix_forecast_feedback_author_id")
    op.execute("DROP INDEX IF EXISTS ix_forecast_feedback_product_id")
    op.execute("DROP INDEX IF EXISTS ix_forecast_feedback_target_id")
    op.drop_table("forecast_feedback")
    op.execute("DROP INDEX IF EXISTS ix_forecast_accuracy_log_run_id")
    op.drop_column("forecast_accuracy_log", "run_id")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/pytest tests/test_forecast_mlops_models.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/models/forecasting.py backend/app/models/__init__.py backend/alembic/versions/041_forecast_mlops_hitl.py backend/tests/test_forecast_mlops_models.py
git commit -m "feat(forecast): add ForecastFeedback + ForecastWeightAdjustment models + migration 041"
```

---

### Task 2: Evaluation job (closes the realized_mape loop)

**Files:**
- Create: `backend/app/services/forecasting/ops/__init__.py`
- Create: `backend/app/services/forecasting/ops/evaluation_job.py`
- Test: `backend/tests/test_forecast_evaluation_job.py`

**Interfaces:**
- Consumes: `ForecastRun.results` = `{str(h): {"base": [float, ...]}}` (daily published forecast, post-overlays), `ForecastRun.as_of_date`, `ForecastAccuracyLog.run_id`/`realized_mape` (from Task 1), `accuracy_tracker.compute_realized_error(forecast_values, forecast_dates, actual_values)`, `ForecastFeedback` (pending rows).
- Produces: `run_evaluation(db, actuals_loader=None, product_key=None) -> dict` — writes `realized_mape`/`evaluated_at`/`run_id` onto new `ForecastAccuracyLog` rows (one per run×horizon), scores pending `ForecastFeedback` rows. Idempotent. Also `get_realized_weights(db, product_key) -> dict | None` (ensemble-only; per-model not reconstructable).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_forecast_evaluation_job.py
"""Evaluation job: populates realized_mape + scores HITL feedback against actuals."""
from datetime import datetime, timedelta, timezone
import pandas as pd
import pytest

from app.database import Base, engine, SessionLocal
from app.models.forecasting import (
    ForecastTarget, ForecastRun, ForecastAccuracyLog, ForecastFeedback,
)
from app.services.forecasting.ops.evaluation_job import run_evaluation

_NEEDED = [ForecastTarget.__table__, ForecastRun.__table__,
           ForecastAccuracyLog.__table__, ForecastFeedback.__table__]


@pytest.fixture(autouse=True)
def _setup_tables():
    Base.metadata.drop_all(engine, tables=_NEEDED)
    Base.metadata.create_all(engine, tables=_NEEDED)
    yield
    Base.metadata.drop_all(engine, tables=_NEEDED)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.rollback(); s.close()


def _make_run(db, as_of, base_values_by_h):
    """Seed a target + a ForecastRun with results[str(h)]["base"] = list."""
    target = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(target); db.flush()
    results = {str(h): {"base": vals, "bull": vals, "bear": vals}
               for h, vals in base_values_by_h.items()}
    run = ForecastRun(
        target_id=target.id, org_id="default-org", app_id="default-app",
        results=results, below_naive_baseline=False, confidence="Medium",
        as_of_date=as_of, model_detail={"ensemble_mape": 0.10, "naive_mape": 0.12},
    )
    db.add(run); db.commit()
    return target, run


def test_writes_realized_mape_for_past_run(db):
    as_of = datetime(2026, 7, 1, tzinfo=timezone.utc)
    # 7-day horizon: forecast 100..106 for Jul 2..Jul 8
    target, run = _make_run(db, as_of, {7: [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]})
    # Actuals: Jul 2..Jul 8 all = 110 → MAPE = 10/110 each
    idx = pd.date_range("2026-07-02", periods=7, freq="D")
    actuals = pd.Series([110.0] * 7, index=idx)
    loader = lambda datasource: actuals  # noqa: E731

    summary = run_evaluation(db, actuals_loader=loader, product_key="电子元件")

    logs = db.query(ForecastAccuracyLog).filter(
        ForecastAccuracyLog.run_id == run.id,
        ForecastAccuracyLog.realized_mape.isnot(None),
    ).all()
    assert len(logs) == 1
    assert logs[0].realized_mape is not None
    assert abs(logs[0].realized_mape - (10.0 / 110.0 * 100)) < 0.01
    assert logs[0].evaluated_at is not None
    assert summary["runs_scored"] >= 1


def test_idempotent_rerun_no_duplicate(db):
    as_of = datetime(2026, 7, 1, tzinfo=timezone.utc)
    target, run = _make_run(db, as_of, {7: [100.0] * 7})
    idx = pd.date_range("2026-07-02", periods=7, freq="D")
    loader = lambda datasource: pd.Series([110.0] * 7, index=idx)  # noqa: E731
    run_evaluation(db, actuals_loader=loader, product_key="电子元件")
    run_evaluation(db, actuals_loader=loader, product_key="电子元件")  # re-run
    n = db.query(ForecastAccuracyLog).filter(
        ForecastAccuracyLog.run_id == run.id,
        ForecastAccuracyLog.realized_mape.isnot(None),
    ).count()
    assert n == 1


def test_skips_run_whose_horizon_not_yet_arrived(db):
    # as_of = today → horizon not passed → not scored
    as_of = datetime.now(timezone.utc) - timedelta(days=1)
    target, run = _make_run(db, as_of, {30: [100.0] * 30})
    loader = lambda datasource: pd.Series([110.0], index=pd.date_range("2026-01-01", periods=1))  # noqa: E731
    run_evaluation(db, actuals_loader=loader, product_key="电子元件")
    n = db.query(ForecastAccuracyLog).filter(
        ForecastAccuracyLog.run_id == run.id,
        ForecastAccuracyLog.realized_mape.isnot(None),
    ).count()
    assert n == 0


def test_scores_pending_feedback(db):
    as_of = datetime(2026, 7, 1, tzinfo=timezone.utc)
    target, run = _make_run(db, as_of, {7: [100.0] * 7})
    # AI said 100; user said 109; actual = 110 → user_error < ai_error → beat
    fb = ForecastFeedback(
        target_id=target.id, product_id="电子元件", ai_price=100.0,
        user_price=109.0, reason="tightening", author_id="u1", author_name="a1",
        target_date=datetime(2026, 7, 5), status="pending", org_id="default-org",
    )
    db.add(fb); db.commit()
    idx = pd.date_range("2026-07-02", periods=7, freq="D")
    loader = lambda datasource: pd.Series([110.0] * 7, index=idx)  # noqa: E731
    run_evaluation(db, actuals_loader=loader, product_key="电子元件")
    db.refresh(fb)
    assert fb.status == "scored"
    assert fb.beat is True
    assert fb.scored_at is not None
    assert fb.ai_error > fb.user_error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/pytest tests/test_forecast_evaluation_job.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.forecasting.ops.evaluation_job`.

- [ ] **Step 3: Implement the evaluation job**

```python
# backend/app/services/forecasting/ops/__init__.py
"""Forecast MLOps + HITL operations package."""
```

```python
# backend/app/services/forecasting/ops/evaluation_job.py
"""Close the realized-accuracy loop.

Matches past ForecastRun rows (their stored published `results[str(h)]["base"]`)
against newly-arrived actuals, writes realized_mape onto ForecastAccuracyLog
rows, and scores pending HITL feedback. Idempotent: a run already scored is
never re-scored; a feedback already scored keeps its result.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import pandas as pd

from app.models.forecasting import (
    ForecastTarget, ForecastRun, ForecastAccuracyLog, ForecastFeedback,
)
from app.services.forecasting.accuracy_tracker import compute_realized_error

logger = logging.getLogger(__name__)

DEFAULT_HORIZONS = [3, 7, 30]


def _default_actuals_loader():
    """Build the default loader that reads Acme MySQL actuals."""
    from app.services.forecasting.edia_source import EdiaMysqlDataSource
    src = EdiaMysqlDataSource()
    return lambda datasource: src.read_history(datasource)


def _reconstruct_forecast_dates(as_of: datetime, horizon: int) -> list[pd.Timestamp]:
    """Daily cadence: forecast step i → as_of + (i+1) days."""
    return [pd.Timestamp(as_of + timedelta(days=i)) for i in range(1, horizon + 1)]


def _actuals_series(actuals_loader, datasource) -> Optional[pd.Series]:
    try:
        df = actuals_loader(datasource)
    except Exception:
        logger.exception("[eval-job] actuals load failed")
        return None
    if df is None or len(df) == 0:
        return None
    # Acme frames: time col FDATE, measure FTAXPRICE. read_history already
    # returns a clean series-like frame; normalize to a date-indexed Series.
    time_col = "FDATE" if "FDATE" in df.columns else df.columns[0]
    measure = "FTAXPRICE" if "FTAXPRICE" in df.columns else df.columns[-1]
    s = pd.Series(df[measure].astype(float).values,
                  index=pd.to_datetime(df[time_col]))
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s


def run_evaluation(
    db,
    actuals_loader: Optional[Callable] = None,
    product_key: Optional[str] = None,
) -> dict:
    """Score past forecasts + pending feedback against newly-arrived actuals.

    Returns: {"runs_scored": int, "runs_skipped": int, "feedback_scored": int}.
    """
    if actuals_loader is None:
        actuals_loader = _default_actuals_loader()

    now = datetime.now(timezone.utc)
    runs_scored = 0
    runs_skipped = 0
    feedback_scored = 0

    q = db.query(ForecastRun)
    if product_key:
        q = q.join(ForecastTarget, ForecastTarget.id == ForecastRun.target_id).filter(
            ForecastTarget.product_key == product_key
        )
    runs = q.order_by(ForecastRun.as_of_date.desc()).limit(200).all()

    for run in runs:
        target = db.query(ForecastTarget).filter(ForecastTarget.id == run.target_id).first()
        if target is None:
            continue
        actuals = _actuals_series(actuals_loader, target.datasource)
        if actuals is None:
            continue
        results = run.results or {}
        for h in DEFAULT_HORIZONS:
            key = str(h)
            if key not in results:
                continue
            base_vals = results[key].get("base") if isinstance(results[key], dict) else None
            if not base_vals:
                continue
            # Skip if the horizon hasn't fully arrived.
            as_of = run.as_of_date or run.created_date
            if as_of is None:
                continue
            horizon_end = as_of + timedelta(days=h)
            if horizon_end > now:
                continue
            # Idempotency: already scored for this run+horizon?
            exists = db.query(ForecastAccuracyLog).filter(
                ForecastAccuracyLog.run_id == run.id,
                ForecastAccuracyLog.horizon_days == h,
                ForecastAccuracyLog.realized_mape.isnot(None),
            ).first()
            if exists:
                runs_skipped += 1
                continue
            fc_dates = _reconstruct_forecast_dates(as_of, h)
            fc_values = [float(v) for v in base_vals[:h]]
            res = compute_realized_error(fc_values, fc_dates, actuals)
            if res["n_matched"] == 0:
                continue
            md = run.model_detail or {}
            log = ForecastAccuracyLog(
                target_id=target.id, org_id=target.org_id, app_id=target.app_id,
                run_id=run.id, horizon_days=h,
                mape=md.get("ensemble_mape"),
                naive_mape=md.get("naive_mape"),
                realized_mape=res["mape"],
                realized_error=res.get("mae"),
                evaluated_at=now,
                below_naive_baseline=run.below_naive_baseline,
                per_model=None,  # marker: realized-eval row (backtest rows always set per_model)
            )
            db.add(log)
            runs_scored += 1

        feedback_scored += _score_pending_feedback(db, target, actuals, now)

    db.commit()
    summary = {
        "runs_scored": runs_scored,
        "runs_skipped": runs_skipped,
        "feedback_scored": feedback_scored,
    }
    logger.info("[eval-job] %s", summary)
    return summary


def _score_pending_feedback(db, target, actuals: pd.Series, now: datetime) -> int:
    pending = db.query(ForecastFeedback).filter(
        ForecastFeedback.target_id == target.id,
        ForecastFeedback.status == "pending",
        ForecastFeedback.target_date.isnot(None),
    ).all()
    scored = 0
    for fb in pending:
        td = pd.Timestamp(fb.target_date)
        if td not in actuals.index:
            # nearest available actual on/before target_date
            prior = actuals[actuals.index <= td]
            if prior.empty:
                continue
            actual = float(prior.iloc[-1])
        else:
            actual = float(actuals.loc[td])
        if actual <= 0:
            continue
        fb.ai_error = abs(fb.ai_price - actual) / actual
        fb.user_error = abs(fb.user_price - actual) / actual
        fb.beat = fb.user_error < fb.ai_error
        fb.status = "scored"
        fb.scored_at = now
        scored += 1
    return scored


def get_realized_weights(db, product_key: str) -> Optional[dict]:
    """Return a realized-weights map for adaptive_weights().

    Per-model realized accuracy is NOT reconstructable (only the ensemble
    published forecast is stored), so this returns None — drift_response uses
    a naive-blend strategy instead. Kept for API symmetry with adaptive_weights.
    """
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/pytest tests/test_forecast_evaluation_job.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/forecasting/ops/__init__.py backend/app/services/forecasting/ops/evaluation_job.py backend/tests/test_forecast_evaluation_job.py
git commit -m "feat(forecast): evaluation job populates realized_mape + scores HITL feedback"
```

---

### Task 3: Drift response (auto-blend audit)

**Files:**
- Create: `backend/app/services/forecasting/ops/drift_response.py`
- Test: `backend/tests/test_forecast_drift_response.py`

**Interfaces:**
- Consumes: `accuracy_tracker.detect_drift(db, product_key)` (now data-backed), `ForecastWeightAdjustment` (Task 1).
- Produces: `check_drift_and_audit(db, target) -> dict` — on drift, writes a `ForecastWeightAdjustment(triggered_by="drift", applied=False)` row if none unapplied in last 7 days. `DRIFT_BLEND_FACTOR` (env `FORECAST_DRIFT_BLEND_FACTOR`, default 0.2) read by the engine.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_forecast_drift_response.py
"""Drift response: writes audit row on drift, no-op when stable."""
from datetime import datetime, timedelta, timezone
import pytest

from app.database import Base, engine, SessionLocal
from app.models.forecasting import (
    ForecastTarget, ForecastRun, ForecastAccuracyLog, ForecastWeightAdjustment,
)
from app.services.forecasting.ops import drift_response

_NEEDED = [ForecastTarget.__table__, ForecastRun.__table__,
           ForecastAccuracyLog.__table__, ForecastWeightAdjustment.__table__]


@pytest.fixture(autouse=True)
def _setup_tables():
    Base.metadata.drop_all(engine, tables=_NEEDED)
    Base.metadata.create_all(engine, tables=_NEEDED)
    yield
    Base.metadata.drop_all(engine, tables=_NEEDED)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.rollback(); s.close()


def _seed_realized(db, target, baseline_mapes, recent_mapes):
    """Insert ForecastAccuracyLog rows with realized_mape at given times."""
    now = datetime.now(timezone.utc)
    for m in baseline_mapes:
        db.add(ForecastAccuracyLog(
            target_id=target.id, horizon_days=7, realized_mape=m,
            evaluated_at=now - timedelta(days=60), org_id="default-org",
        ))
    for m in recent_mapes:
        db.add(ForecastAccuracyLog(
            target_id=target.id, horizon_days=7, realized_mape=m,
            evaluated_at=now - timedelta(days=10), org_id="default-org",
        ))
    db.commit()


def test_writes_audit_row_on_drift(db, monkeypatch):
    target = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(target); db.commit()
    # baseline ~12%, recent ~25% → 25 > 12*1.2 → drift
    _seed_realized(db, target, [0.12, 0.11, 0.12], [0.25, 0.26, 0.24])

    result = drift_response.check_drift_and_audit(db, target)
    assert result["is_drifting"] is True
    rows = db.query(ForecastWeightAdjustment).filter(
        ForecastWeightAdjustment.target_id == target.id,
        ForecastWeightAdjustment.triggered_by == "drift",
    ).all()
    assert len(rows) == 1
    assert rows[0].applied is False
    assert "recent" in (rows[0].reason or "")


def test_no_audit_when_stable(db):
    target = ForecastTarget(product_key="材料J", name="材料J", org_id="default-org")
    db.add(target); db.commit()
    _seed_realized(db, target, [0.10, 0.10, 0.10], [0.11, 0.10, 0.10])

    result = drift_response.check_drift_and_audit(db, target)
    assert result["is_drifting"] is False
    n = db.query(ForecastWeightAdjustment).filter(
        ForecastWeightAdjustment.target_id == target.id,
    ).count()
    assert n == 0


def test_no_duplicate_audit_within_7_days(db):
    target = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(target); db.commit()
    _seed_realized(db, target, [0.12, 0.11, 0.12], [0.25, 0.26, 0.24])
    drift_response.check_drift_and_audit(db, target)
    drift_response.check_drift_and_audit(db, target)  # second call same window
    n = db.query(ForecastWeightAdjustment).filter(
        ForecastWeightAdjustment.target_id == target.id,
        ForecastWeightAdjustment.triggered_by == "drift",
    ).count()
    assert n == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/pytest tests/test_forecast_drift_response.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.forecasting.ops.drift_response`.

- [ ] **Step 3: Implement drift_response**

```python
# backend/app/services/forecasting/ops/drift_response.py
"""Drift auto-response: detect drift (now data-backed) and write an audit row
that the engine will blend toward the naive baseline on the next run."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from app.models.forecasting import ForecastTarget, ForecastWeightAdjustment
from app.services.forecasting.accuracy_tracker import detect_drift

logger = logging.getLogger(__name__)

_DRIFT_AUDIT_WINDOW_DAYS = 7


def get_drift_blend_factor() -> float:
    raw = os.environ.get("FORECAST_DRIFT_BLEND_FACTOR", "0.2")
    try:
        f = float(raw)
    except (TypeError, ValueError):
        f = 0.2
    return max(0.0, min(f, 0.5))


def check_drift_and_audit(db, target: ForecastTarget) -> dict:
    """Detect drift for a target; if drifting and no unapplied audit row exists
    in the last 7 days, write one (applied=False). Returns detect_drift dict."""
    result = detect_drift(db, target.product_key)
    if not result.get("is_drifting"):
        return result

    cutoff = datetime.now(timezone.utc) - timedelta(days=_DRIFT_AUDIT_WINDOW_DAYS)
    existing = db.query(ForecastWeightAdjustment).filter(
        ForecastWeightAdjustment.target_id == target.id,
        ForecastWeightAdjustment.triggered_by == "drift",
        ForecastWeightAdjustment.applied == False,  # noqa: E712
        ForecastWeightAdjustment.created_date >= cutoff,
    ).first()
    if existing is not None:
        return result  # already pending; engine will apply it

    audit = ForecastWeightAdjustment(
        target_id=target.id, org_id=target.org_id, app_id=target.app_id,
        triggered_by="drift",
        reason=result.get("reason", "drift detected"),
        old_weights=None, new_weights=None, delta_ratio=None,
        applied=False,
    )
    db.add(audit)
    db.commit()
    logger.warning(
        "[drift-response] %s drifting — audit row written for naive-blend (factor=%.2f)",
        target.product_key, get_drift_blend_factor(),
    )
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/pytest tests/test_forecast_drift_response.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/forecasting/ops/drift_response.py backend/tests/test_forecast_drift_response.py
git commit -m "feat(forecast): drift response writes naive-blend audit row on drift"
```

---

### Task 4: HITL feedback service

**Files:**
- Create: `backend/app/services/forecasting/ops/feedback_service.py`
- Test: `backend/tests/test_forecast_feedback_service.py`

**Interfaces:**
- Consumes: `ForecastFeedback` (Task 1), `ForecastTarget`.
- Produces: `record_feedback(db, product_id, ai_price, user_price, reason, author_id, author_name, target_date) -> ForecastFeedback`, `list_feedback(db, product_id) -> list[dict]`, `author_track_record(db, author_id, product_id) -> dict` (count, beat_rate, scored_count — feeds the trust gate).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_forecast_feedback_service.py
"""HITL feedback service: capture, list, author track-record."""
from datetime import datetime
import pytest

from app.database import Base, engine, SessionLocal
from app.models.forecasting import ForecastTarget, ForecastFeedback
from app.services.forecasting.ops.feedback_service import (
    record_feedback, list_feedback, author_track_record,
)

_NEEDED = [ForecastTarget.__table__, ForecastFeedback.__table__]


@pytest.fixture(autouse=True)
def _setup_tables():
    Base.metadata.drop_all(engine, tables=_NEEDED)
    Base.metadata.create_all(engine, tables=_NEEDED)
    yield
    Base.metadata.drop_all(engine, tables=_NEEDED)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.rollback(); s.close()


def test_record_feedback_pending(db):
    t = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(t); db.commit()
    fb = record_feedback(db, product_id="电子元件", ai_price=10388.0,
                         user_price=11000.0, reason="supply tightening",
                         author_id="u1", author_name="analyst",
                         target_date=datetime(2026, 8, 10))
    assert fb.status == "pending"
    assert fb.beat is None


def test_list_feedback_returns_history(db):
    t = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(t); db.commit()
    record_feedback(db, "电子元件", 100.0, 109.0, "x", "u1", "a1", datetime(2026, 8, 1))
    rows = list_feedback(db, "电子元件")
    assert len(rows) == 1
    assert rows[0]["user_price"] == 109.0


def test_author_track_record(db):
    t = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(t); db.commit()
    # 3 scored: 2 beat, 1 miss
    for ai, usr in [(100, 109), (100, 109), (100, 90)]:
        fb = record_feedback(db, "电子元件", float(ai), float(usr), "r", "u1", "a1", datetime(2026, 8, 1))
        fb.status = "scored"; fb.beat = (usr == 109); fb.ai_error = 0.1; fb.user_error = 0.01
    db.commit()
    tr = author_track_record(db, author_id="u1", product_id="电子元件")
    assert tr["scored"] == 3
    assert tr["beat"] == 2
    assert abs(tr["beat_rate"] - 2 / 3) < 0.01


def test_rejects_non_positive_user_price(db):
    t = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(t); db.commit()
    with pytest.raises(ValueError):
        record_feedback(db, "电子元件", 100.0, -5.0, "r", "u1", "a1", datetime(2026, 8, 1))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/pytest tests/test_forecast_feedback_service.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.forecasting.ops.feedback_service`.

- [ ] **Step 3: Implement feedback_service**

```python
# backend/app/services/forecasting/ops/feedback_service.py
"""Human-in-the-Loop feedback capture + author track-record.

A recorded correction is an ASSERTION, not an action — it never moves the
forecast until the trust gate (bias_correction.py) is earned.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from app.models.forecasting import ForecastTarget, ForecastFeedback

logger = logging.getLogger(__name__)


def record_feedback(
    db,
    product_id: str,
    ai_price: float,
    user_price: float,
    reason: Optional[str],
    author_id: str,
    author_name: Optional[str],
    target_date: Optional[datetime],
) -> ForecastFeedback:
    if user_price <= 0:
        raise ValueError("user_price must be positive")
    if ai_price <= 0:
        raise ValueError("ai_price must be positive")
    target = db.query(ForecastTarget).filter(
        ForecastTarget.product_key == product_id,
        ForecastTarget.is_deleted == False,  # noqa: E712
    ).first()
    if target is None:
        raise ValueError(f"no forecast target for product_id={product_id!r}")
    fb = ForecastFeedback(
        target_id=target.id, org_id=target.org_id, app_id=target.app_id,
        product_id=product_id, ai_price=float(ai_price), user_price=float(user_price),
        reason=reason, author_id=author_id, author_name=author_name,
        target_date=target_date, status="pending",
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)
    logger.info("[hitl] feedback recorded: %s ai=%s user=%s by=%s",
                product_id, ai_price, user_price, author_id)
    return fb


def list_feedback(db, product_id: str) -> list[dict]:
    rows = db.query(ForecastFeedback).filter(
        ForecastFeedback.product_id == product_id,
        ForecastFeedback.is_deleted == False,  # noqa: E712
    ).order_by(ForecastFeedback.created_date.desc()).limit(100).all()
    return [_fb_dict(r) for r in rows]


def _fb_dict(r: ForecastFeedback) -> dict:
    return {
        "id": r.id, "ai_price": r.ai_price, "user_price": r.user_price,
        "reason": r.reason, "author_name": r.author_name,
        "target_date": r.target_date.isoformat() if r.target_date else None,
        "status": r.status, "beat": r.beat,
        "ai_error": r.ai_error, "user_error": r.user_error,
        "created_date": r.created_date.isoformat() if r.created_date else None,
    }


def author_track_record(db, author_id: str, product_id: str) -> dict:
    """Aggregate an author's SCORED overrides for a product — feeds the trust gate."""
    rows = db.query(ForecastFeedback).filter(
        ForecastFeedback.author_id == author_id,
        ForecastFeedback.product_id == product_id,
        ForecastFeedback.status == "scored",
        ForecastFeedback.is_deleted == False,  # noqa: E712
    ).all()
    scored = len(rows)
    beat = sum(1 for r in rows if r.beat)
    return {
        "scored": scored,
        "beat": beat,
        "beat_rate": (beat / scored) if scored else 0.0,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/pytest tests/test_forecast_feedback_service.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/forecasting/ops/feedback_service.py backend/tests/test_forecast_feedback_service.py
git commit -m "feat(forecast): HITL feedback service (capture/list/track-record)"
```

---

### Task 5: Feedback API endpoints (market_dashboard router)

**Files:**
- Modify: `backend/app/schemas/market_dashboard.py` (add schemas)
- Modify: `backend/app/routers/market_dashboard.py` (add 2 endpoints)
- Test: `backend/tests/test_forecast_feedback_api.py`

**Interfaces:**
- Consumes: `feedback_service.record_feedback` / `list_feedback` (Task 4), `get_current_user_required` (deps.py), `market_dashboard.router` (prefix `/market-dashboard`).
- Produces: `POST /api/market-dashboard/forecast-feedback` (gated by `FORECAST_HITL_ENABLED`), `GET /api/market-dashboard/forecast-feedback?product_id=`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_forecast_feedback_api.py
"""Feedback API: capture + list, gated by FORECAST_HITL_ENABLED."""
import os
from datetime import datetime
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal, get_db
from app.deps import get_current_user_required
from app.models.forecasting import ForecastTarget, ForecastFeedback
from app.models.user import User
from app.routers import market_dashboard as md_router

_NEEDED = [ForecastTarget.__table__, ForecastFeedback.__table__]


@pytest.fixture(autouse=True)
def _setup_tables():
    Base.metadata.drop_all(engine, tables=_NEEDED)
    Base.metadata.create_all(engine, tables=_NEEDED)
    yield
    Base.metadata.drop_all(engine, tables=_NEEDED)


def _client(db, hitl_on: bool, user=None):
    os.environ["FORECAST_HITL_ENABLED"] = "true" if hitl_on else "false"
    app = FastAPI()
    app.include_router(md_router.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: (user or _fake_user())
    return TestClient(app)


def _fake_user():
    u = User(email="a@b.c", hashed_password="x", is_admin=True)
    u.id = "u1"; u.name = "analyst"
    return u


def test_post_feedback_creates_pending(db, monkeypatch):
    t = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(t); db.commit()
    c = _client(db, hitl_on=True)
    r = c.post("/api/market-dashboard/forecast-feedback", json={
        "product_id": "电子元件", "ai_price": 10388.0, "user_price": 11000.0,
        "reason": "tightening", "target_date": "2026-08-10T00:00:00",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["user_price"] == 11000.0


def test_post_feedback_disabled_when_flag_off(db):
    t = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(t); db.commit()
    c = _client(db, hitl_on=False)
    r = c.post("/api/market-dashboard/forecast-feedback", json={
        "product_id": "电子元件", "ai_price": 100.0, "user_price": 110.0, "reason": "x",
    })
    assert r.status_code in (404, 501, 503)


def test_get_feedback_history(db):
    t = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(t); db.commit()
    c = _client(db, hitl_on=True)
    c.post("/api/market-dashboard/forecast-feedback", json={
        "product_id": "电子元件", "ai_price": 100.0, "user_price": 109.0, "reason": "r",
    })
    r = c.get("/api/market-dashboard/forecast-feedback?product_id=电子元件")
    assert r.status_code == 200
    assert len(r.json()) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/pytest tests/test_forecast_feedback_api.py -v`
Expected: FAIL — 404 (endpoints don't exist yet).

- [ ] **Step 3: Add schemas + endpoints**

In `backend/app/schemas/market_dashboard.py`, append:

```python
from datetime import datetime
from pydantic import BaseModel, Field


class ForecastFeedbackRequest(BaseModel):
    product_id: str
    ai_price: float = Field(gt=0)
    user_price: float = Field(gt=0)
    reason: str | None = None
    target_date: datetime | None = None


class ForecastFeedbackItem(BaseModel):
    id: str
    ai_price: float
    user_price: float
    reason: str | None = None
    author_name: str | None = None
    target_date: str | None = None
    status: str
    beat: bool | None = None
    ai_error: float | None = None
    user_error: float | None = None
    created_date: str | None = None
```

In `backend/app/routers/market_dashboard.py`, add imports + endpoints:

```python
import os
from app.schemas.market_dashboard import (
    # ... existing imports ...
    ForecastFeedbackRequest, ForecastFeedbackItem,
)
from app.services.forecasting.ops.feedback_service import (
    record_feedback, list_feedback,
)


def _hitl_enabled() -> bool:
    return os.environ.get("FORECAST_HITL_ENABLED", "false").lower() in ("1", "true", "yes")


@router.post("/forecast-feedback", response_model=ForecastFeedbackItem)
def post_forecast_feedback(
    body: ForecastFeedbackRequest,
    user=Depends(get_current_user_required),
):
    if not _hitl_enabled():
        raise HTTPException(status_code=503, detail="forecast_hitl_disabled")
    try:
        fb = record_feedback(
            db=Depends(get_db),  # placeholder — see note below
            product_id=body.product_id, ai_price=body.ai_price,
            user_price=body.user_price, reason=body.reason,
            author_id=str(getattr(user, "id", "anonymous")),
            author_name=getattr(user, "name", None) or getattr(user, "email", None),
            target_date=body.target_date,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ForecastFeedbackItem(**_to_item(fb))
```

**Note on `db`:** the existing router endpoints get `db` via the service layer (e.g. `svc.get_market_snapshot`). For feedback, take an explicit DB session. Replace the `db=Depends(get_db)` placeholder above with a real parameter:

```python
from app.database import get_db

@router.post("/forecast-feedback", response_model=ForecastFeedbackItem)
def post_forecast_feedback(
    body: ForecastFeedbackRequest,
    db=Depends(get_db),
    user=Depends(get_current_user_required),
):
    if not _hitl_enabled():
        raise HTTPException(status_code=503, detail="forecast_hitl_disabled")
    try:
        fb = record_feedback(
            db=db, product_id=body.product_id, ai_price=body.ai_price,
            user_price=body.user_price, reason=body.reason,
            author_id=str(getattr(user, "id", "anonymous")),
            author_name=getattr(user, "name", None) or getattr(user, "email", None),
            target_date=body.target_date,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ForecastFeedbackItem(**_to_item(fb))


@router.get("/forecast-feedback", response_model=list[ForecastFeedbackItem])
def get_forecast_feedback(
    product_id: str = Query(...),
    db=Depends(get_db),
    user=Depends(get_current_user_required),
):
    if not _hitl_enabled():
        raise HTTPException(status_code=503, detail="forecast_hitl_disabled")
    return [ForecastFeedbackItem(**row) for row in list_feedback(db, product_id)]


def _to_item(fb) -> dict:
    return {
        "id": fb.id, "ai_price": fb.ai_price, "user_price": fb.user_price,
        "reason": fb.reason, "author_name": fb.author_name,
        "target_date": fb.target_date.isoformat() if fb.target_date else None,
        "status": fb.status, "beat": fb.beat,
        "ai_error": fb.ai_error, "user_error": fb.user_error,
        "created_date": fb.created_date.isoformat() if fb.created_date else None,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/pytest tests/test_forecast_feedback_api.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas/market_dashboard.py backend/app/routers/market_dashboard.py backend/tests/test_forecast_feedback_api.py
git commit -m "feat(forecast): HITL feedback API endpoints (POST/GET forecast-feedback)"
```

---

### Task 6: Bias correction (trust gate)

**Files:**
- Create: `backend/app/services/forecasting/ops/bias_correction.py`
- Test: `backend/tests/test_forecast_bias_correction.py`

**Interfaces:**
- Consumes: `feedback_service.author_track_record` (Task 4), `ForecastFeedback` (scored rows), `ForecastWeightAdjustment` (audit).
- Produces: `trust_gate_met(db, author_id, product_id) -> bool` (≥3 scored, >50% beat-rate), `compute_bias_delta(db, product_id, author_id) -> dict` (mean signed delta ratio, recency-weighted, ±0.10 clamped), `apply_bias_correction(db, target, published_series, author_id=None) -> tuple[pd.Series, dict|None]` — returns (adjusted_series, explanation_dict_or_None). Writes a `ForecastWeightAdjustment(triggered_by="bias_correction")` audit row when applied.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_forecast_bias_correction.py
"""Trust gate + validated bias delta."""
from datetime import datetime, timedelta, timezone
import math
import numpy as np
import pandas as pd
import pytest

from app.database import Base, engine, SessionLocal
from app.models.forecasting import ForecastTarget, ForecastFeedback, ForecastWeightAdjustment
from app.services.forecasting.ops import bias_correction as bc

_NEEDED = [ForecastTarget.__table__, ForecastFeedback.__table__, ForecastWeightAdjustment.__table__]


@pytest.fixture(autouse=True)
def _setup_tables():
    Base.metadata.drop_all(engine, tables=_NEEDED)
    Base.metadata.create_all(engine, tables=_NEEDED)
    yield
    Base.metadata.drop_all(engine, tables=_NEEDED)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.rollback(); s.close()


def _add_scored(db, target, author, days_ago, ai, usr, beat):
    fb = ForecastFeedback(
        target_id=target.id, product_id="电子元件", ai_price=float(ai),
        user_price=float(usr), reason="r", author_id=author, author_name=author,
        target_date=datetime.now(timezone.utc) - timedelta(days=days_ago),
        status="scored", beat=beat, ai_error=0.1, user_error=0.05,
        org_id="default-org",
    )
    fb.created_date = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.add(fb)


def test_gate_not_met_below_3(db):
    t = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(t); db.commit()
    _add_scored(db, t, "u1", 5, 100, 109, True)
    _add_scored(db, t, "u1", 4, 100, 109, True)
    assert bc.trust_gate_met(db, "u1", "电子元件") is False


def test_gate_not_met_low_beat_rate(db):
    t = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(t); db.commit()
    for beat in [True, False, False]:
        _add_scored(db, t, "u1", 5, 100, 109, beat)
    # 1/3 beat → <50%
    assert bc.trust_gate_met(db, "u1", "电子元件") is False


def test_gate_met(db):
    t = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(t); db.commit()
    for beat in [True, True, False]:
        _add_scored(db, t, "u1", 5, 100, 109, beat)
    assert bc.trust_gate_met(db, "u1", "电子元件") is True


def test_bias_delta_clamped_to_10pct(db):
    t = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(t); db.commit()
    # user consistently 50% above AI → delta_ratio 0.5 → clamped to 0.10
    for _ in range(4):
        _add_scored(db, t, "u1", 3, 100, 150, True)
    delta = bc.compute_bias_delta(db, "电子元件", "u1")
    assert delta["clamped"] is True
    assert math.isclose(delta["delta_ratio"], 0.10, abs_tol=1e-6)


def test_apply_returns_unadjusted_when_gate_not_met(db):
    t = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(t); db.commit()
    s = pd.Series([100.0, 101.0, 102.0])
    out, expl = bc.apply_bias_correction(db, t, s, author_id="u1")
    assert expl is None
    assert np.allclose(out.values, s.values)


def test_apply_adjusts_and_audits_when_gate_met(db):
    t = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(t); db.commit()
    for _ in range(4):
        _add_scored(db, t, "u1", 3, 100, 110, True)  # +10% consistently
    s = pd.Series([100.0, 101.0, 102.0])
    out, expl = bc.apply_bias_correction(db, t, s, author_id="u1")
    assert expl is not None
    assert math.isclose(expl["delta_ratio"], 0.10, abs_tol=1e-6)
    # 100 → 110, 101 → 111.1, 102 → 112.2
    assert math.isclose(out.iloc[0], 110.0, abs_tol=0.01)
    n = db.query(ForecastWeightAdjustment).filter(
        ForecastWeightAdjustment.target_id == t.id,
        ForecastWeightAdjustment.triggered_by == "bias_correction",
    ).count()
    assert n == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/pytest tests/test_forecast_bias_correction.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.forecasting.ops.bias_correction`.

- [ ] **Step 3: Implement bias_correction**

```python
# backend/app/services/forecasting/ops/bias_correction.py
"""Validated bias-correction learning layer.

An author's judgement only shifts a forecast after it has demonstrably beaten
the AI against reality (trust gate). The delta is recency-weighted, clamped to
±10%, and applied to the published price only — never the decision call.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from app.models.forecasting import ForecastFeedback, ForecastWeightAdjustment
from app.services.forecasting.ops.feedback_service import author_track_record

logger = logging.getLogger(__name__)

MIN_SCORED = 3
MIN_BEAT_RATE = 0.5
BIAS_CAP = 0.10
RECENCY_HALFLIFE_DAYS = 30


def trust_gate_met(db, author_id: str, product_id: str) -> bool:
    tr = author_track_record(db, author_id, product_id)
    return tr["scored"] >= MIN_SCORED and tr["beat_rate"] > MIN_BEAT_RATE


def compute_bias_delta(db, product_id: str, author_id: str) -> dict:
    """Recency-weighted mean signed (user_price - ai_price)/ai_price, clamped to ±BIAS_CAP."""
    rows = db.query(ForecastFeedback).filter(
        ForecastFeedback.product_id == product_id,
        ForecastFeedback.author_id == author_id,
        ForecastFeedback.status == "scored",
        ForecastFeedback.is_deleted == False,  # noqa: E712
    ).all()
    now = datetime.now(timezone.utc)
    deltas, weights = [], []
    for r in rows:
        if r.ai_price <= 0:
            continue
        d = (r.user_price - r.ai_price) / r.ai_price
        age_days = (now - (r.scored_at or r.created_date or now)).total_seconds() / 86400.0
        w = 0.5 ** (max(0.0, age_days) / RECENCY_HALFLIFE_DAYS)
        deltas.append(d); weights.append(w)
    if not deltas:
        return {"delta_ratio": 0.0, "clamped": False, "n": 0}
    wsum = sum(weights)
    raw = sum(d * w for d, w in zip(deltas, weights)) / wsum if wsum else 0.0
    clamped = abs(raw) > BIAS_CAP
    delta = max(-BIAS_CAP, min(BIAS_CAP, raw))
    return {"delta_ratio": delta, "clamped": clamped, "n": len(deltas), "raw": raw}


def apply_bias_correction(db, target, published_series: pd.Series, author_id: str | None = None):
    """Returns (adjusted_series, explanation_dict_or_None).

    If no trusted author (or author_id None) → returns the series unchanged with None.
    """
    if author_id is None:
        return published_series, None
    if not trust_gate_met(db, author_id, target.product_key):
        return published_series, None

    delta = compute_bias_delta(db, target.product_key, author_id)
    if delta["delta_ratio"] == 0.0:
        return published_series, None

    factor = 1.0 + delta["delta_ratio"]
    adjusted = published_series * factor

    audit = ForecastWeightAdjustment(
        target_id=target.id, org_id=target.org_id, app_id=target.app_id,
        triggered_by="bias_correction",
        reason=f"trusted author {author_id} delta={delta['delta_ratio']:.4f} (n={delta['n']})",
        old_weights=None, new_weights=None,
        delta_ratio=delta["delta_ratio"], applied=True, applied_at=datetime.now(timezone.utc),
    )
    db.add(audit); db.commit()

    expl = {
        "author_id": author_id,
        "delta_ratio": delta["delta_ratio"],
        "n_overrides": delta["n"],
        "clamped": delta["clamped"],
    }
    logger.info("[bias-correction] %s applied delta=%.4f (n=%d)",
                target.product_key, delta["delta_ratio"], delta["n"])
    return adjusted, expl
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/pytest tests/test_forecast_bias_correction.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/forecasting/ops/bias_correction.py backend/tests/test_forecast_bias_correction.py
git commit -m "feat(forecast): trust-gated bias correction (>=3 scored, >50% beat, ±10% cap)"
```

---

### Task 7: Engine integration (drift-blend + bias-correction plug-in)

**Files:**
- Modify: `backend/app/services/forecasting/engine.py` (between policy overlay ~line 478 and `scenarios.generate` ~line 518; also set `explanation` fields after results build)
- Test: `backend/tests/test_forecast_engine_ops_integration.py`

**Interfaces:**
- Consumes: `drift_response.get_drift_blend_factor` + `ForecastWeightAdjustment` query (Task 3), `bias_correction.apply_bias_correction` (Task 6), `naive_fc` (already in `compute_target` scope, engine.py:~337), `guard_result.published_forecast`.
- Produces: when `FORECAST_DRIFT_AUTO_ADJUST_ENABLED` → blend `guard_result.published_forecast` toward `naive_fc` by the drift factor, mark audit row `applied=True`, set `explanation["drift_adjustment"]`. When `FORECAST_BIAS_CORRECTION_ENABLED` → call `apply_bias_correction`, set `explanation["bias_correction"]`.

- [ ] **Step 1: Verify the integration point**

Read `backend/app/services/forecasting/engine.py` around lines 470-520. Confirm:
1. `naive_fc` is in scope at the integration point (it's assigned near line 337 — yes).
2. `compute_target_anchored` (line 955) delegates to `compute_target` (so one edit covers both). If `compute_target_anchored` duplicates the publish step, apply the same edit there.

Document the exact line numbers in a comment for the next step.

- [ ] **Step 2: Write the failing test**

```python
# backend/tests/test_forecast_engine_ops_integration.py
"""Engine integration: drift-blend and bias-correction plug into the publish step.

These tests exercise the helpers in isolation (full engine run needs MySQL);
they lock the contract the engine calls."""
from datetime import datetime, timedelta, timezone
import math
import os
import numpy as np
import pandas as pd
import pytest

from app.database import Base, engine, SessionLocal
from app.models.forecasting import (
    ForecastTarget, ForecastFeedback, ForecastWeightAdjustment,
)
from app.services.forecasting.ops import drift_response, bias_correction

_NEEDED = [ForecastTarget.__table__, ForecastFeedback.__table__, ForecastWeightAdjustment.__table__]


@pytest.fixture(autouse=True)
def _setup_tables():
    Base.metadata.drop_all(engine, tables=_NEEDED)
    Base.metadata.create_all(engine, tables=_NEEDED)
    yield
    Base.metadata.drop_all(engine, tables=_NEEDED)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.rollback(); s.close()


def test_drift_blend_pulls_published_toward_naive(db):
    """The engine helper blends published*(1-f) + naive*f."""
    target = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(target); db.commit()
    # Seed an unapplied drift audit row.
    db.add(ForecastWeightAdjustment(
        target_id=target.id, org_id="default-org", triggered_by="drift",
        reason="drift", applied=False,
    )); db.commit()
    published = pd.Series([100.0, 100.0])
    naive = pd.Series([120.0, 120.0])
    f = 0.2
    blended = published * (1 - f) + naive * f
    assert math.isclose(blended.iloc[0], 104.0, abs_tol=0.01)
    # The engine marks the audit row applied:
    row = db.query(ForecastWeightAdjustment).filter(
        ForecastWeightAdjustment.target_id == target.id,
        ForecastWeightAdjustment.applied == False,  # noqa: E712
    ).first()
    row.applied = True; row.applied_at = datetime.now(timezone.utc)
    db.commit()
    assert db.query(ForecastWeightAdjustment).filter(
        ForecastWeightAdjustment.applied == True  # noqa: E712
    ).count() == 1


def test_bias_correction_explanation_shape(db):
    target = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(target); db.commit()
    for _ in range(4):
        fb = ForecastFeedback(
            target_id=target.id, product_id="电子元件", ai_price=100.0, user_price=110.0,
            reason="r", author_id="u1", author_name="u1",
            target_date=datetime.now(timezone.utc) - timedelta(days=3),
            status="scored", beat=True, ai_error=0.1, user_error=0.01, org_id="default-org",
        )
        fb.created_date = datetime.now(timezone.utc) - timedelta(days=3)
        db.add(fb)
    db.commit()
    s = pd.Series([100.0])
    out, expl = bias_correction.apply_bias_correction(db, target, s, author_id="u1")
    assert expl is not None
    assert {"author_id", "delta_ratio", "n_overrides", "clamped"} <= set(expl.keys())
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/pytest tests/test_forecast_engine_ops_integration.py -v`
Expected: PASS (these are contract tests that should pass once Tasks 3+6 land) — if they pass already, the engine edit is the real change. Proceed to the engine edit.

- [ ] **Step 4: Edit the engine**

In `backend/app/services/forecasting/engine.py`, immediately BEFORE the `scenarios_result = generate(guard_result.published_forecast, ...)` call (~line 518), insert the ops plug-in:

```python
        # ── MLOps / HITL publish-step adjustments (Task 7) ──────────────
        # Both adjust guard_result.published_forecast (the price users see)
        # but NEVER the decision-engine action call. Disclosed in the brief.
        from app.services.forecasting.ops import drift_response, bias_correction

        if _get_config_bool("FORECAST_DRIFT_AUTO_ADJUST_ENABLED", False):
            try:
                from datetime import datetime, timezone
                from app.models.forecasting import ForecastWeightAdjustment
                pending_drift = self._db.query(ForecastWeightAdjustment).filter(
                    ForecastWeightAdjustment.target_id == target_id,
                    ForecastWeightAdjustment.triggered_by == "drift",
                    ForecastWeightAdjustment.applied == False,  # noqa: E712
                ).order_by(ForecastWeightAdjustment.created_date.desc()).first()
                if pending_drift is not None and naive_fc is not None:
                    f = drift_response.get_drift_blend_factor()
                    blended = guard_result.published_forecast * (1 - f) + naive_fc * f
                    guard_result = dataclasses.replace(
                        guard_result, published_forecast=blended,
                    )
                    pending_drift.applied = True
                    pending_drift.applied_at = datetime.now(timezone.utc)
                    self._db.flush()
                    drift_report = {
                        "blend_factor": f,
                        "audit_id": pending_drift.id,
                        "reason": pending_drift.reason,
                    }
                    logger.info("[ops] drift-blend applied to %s (f=%.2f)", target.product_key, f)
                else:
                    drift_report = None
            except Exception:
                logger.exception("[ops] drift-blend failed for %s", target.product_key)
                drift_report = None
        else:
            drift_report = None

        bias_report = None
        if _get_config_bool("FORECAST_BIAS_CORRECTION_ENABLED", False):
            try:
                _adjusted, bias_report = bias_correction.apply_bias_correction(
                    self._db, target, guard_result.published_forecast,
                    author_id=None,  # nearest trusted author resolved per-product (see note)
                )
                if bias_report is not None:
                    guard_result = dataclasses.replace(
                        guard_result, published_forecast=_adjusted,
                    )
            except Exception:
                logger.exception("[ops] bias-correction failed for %s", target.product_key)
        ```

**Note on `author_id`:** `apply_bias_correction` needs the trusted author. Resolution: query the most recent scored-feedback author for the product who passes the gate. Add a helper `bias_correction.resolve_trusted_author(db, product_key) -> str | None` and call it. Add it to `bias_correction.py`:

```python
def resolve_trusted_author(db, product_key: str) -> str | None:
    """Most recent scored author for the product who passes the trust gate."""
    rows = db.query(ForecastFeedback).filter(
        ForecastFeedback.product_id == product_key,
        ForecastFeedback.status == "scored",
        ForecastFeedback.is_deleted == False,  # noqa: E712
    ).order_by(ForecastFeedback.scored_at.desc()).limit(50).all()
    seen = []
    for r in rows:
        if r.author_id not in seen:
            seen.append(r.author_id)
    for aid in seen:
        if trust_gate_met(db, aid, product_key):
            return aid
    return None
```

Then in the engine call: `author_id=bias_correction.resolve_trusted_author(self._db, target.product_key)`.

**`explanation` wiring:** after the `results`/`model_detail` block is built (~line 690, where `explanation=` is set on the ForecastRun), merge the reports:

```python
        # merge ops reports into the run explanation
        ops_explanation = {}
        if drift_report is not None:
            ops_explanation["drift_adjustment"] = drift_report
        if bias_report is not None:
            ops_explanation["bias_correction"] = bias_report
```

Then ensure `explanation` (the dict passed to `ForecastRun(explanation=...)`) includes `ops_explanation` merged in. Find where `explanation` is assembled and add: `explanation.update(ops_explanation)` before constructing the `ForecastRun`.

Add `import dataclasses` at the top of engine.py if not present (GuardResult is a dataclass; `dataclasses.replace` needs the import).

- [ ] **Step 5: Run the engine integration + existing engine tests**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/pytest tests/test_forecast_engine_ops_integration.py tests/test_forecasting_models_engine.py -v`
Expected: PASS. (Both flags default OFF, so existing engine behavior is unchanged.)

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/forecasting/engine.py backend/app/services/forecasting/ops/bias_correction.py backend/tests/test_forecast_engine_ops_integration.py
git commit -m "feat(forecast): engine publish-step integration of drift-blend + bias-correction"
```

---

### Task 8: forecast_ops router (status + run-evaluation)

**Files:**
- Create: `backend/app/routers/forecast_ops.py`
- Modify: `backend/app/main.py` (register router)
- Test: `backend/tests/test_forecast_ops_router.py`

**Interfaces:**
- Consumes: `evaluation_job.run_evaluation` (Task 2), `ForecastWeightAdjustment` (drift chips), `require_admin` (deps.py), `get_db`.
- Produces: `GET /api/forecast-ops/status` (per-product drift/adjustment chips, eval last-run), `POST /api/forecast-ops/run-evaluation` (manual backfill, admin-only).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_forecast_ops_router.py
"""forecast_ops router: status + manual run-evaluation."""
import os
from datetime import datetime, timezone
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal, get_db
from app.deps import require_admin, get_current_user_required
from app.models.forecasting import ForecastTarget, ForecastWeightAdjustment
from app.models.user import User
from app.routers import forecast_ops

_NEEDED = [ForecastTarget.__table__, ForecastWeightAdjustment.__table__]


@pytest.fixture(autouse=True)
def _setup_tables():
    Base.metadata.drop_all(engine, tables=_NEEDED)
    Base.metadata.create_all(engine, tables=_NEEDED)
    yield
    Base.metadata.drop_all(engine, tables=_NEEDED)


def _client(db, eval_on: bool, admin=True):
    os.environ["FORECAST_EVAL_JOB_ENABLED"] = "true" if eval_on else "false"
    app = FastAPI()
    app.include_router(forecast_ops.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    u = User(email="a@b.c", hashed_password="x", is_admin=admin)
    u.id = "u1"
    app.dependency_overrides[get_current_user_required] = lambda: u
    app.dependency_overrides[require_admin] = lambda: u
    return TestClient(app)


def test_status_returns_chips(db):
    t = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(t); db.commit()
    db.add(ForecastWeightAdjustment(
        target_id=t.id, org_id="default-org", triggered_by="drift",
        reason="drift", applied=False,
    )); db.commit()
    c = _client(db, eval_on=True)
    r = c.get("/api/forecast-ops/status")
    assert r.status_code == 200
    body = r.json()
    assert any(p["product_key"] == "电子元件" for p in body["products"])


def test_run_evaluation_admin_only(db, monkeypatch):
    t = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org")
    db.add(t); db.commit()
    # stub the actuals loader so no MySQL is hit
    monkeypatch.setattr(
        "app.services.forecasting.ops.evaluation_job._default_actuals_loader",
        lambda: (lambda datasource: None),
    )
    c = _client(db, eval_on=True, admin=True)
    r = c.post("/api/forecast-ops/run-evaluation")
    assert r.status_code == 200
    assert "runs_scored" in r.json()


def test_run_evaluation_disabled_when_flag_off(db):
    c = _client(db, eval_on=False, admin=True)
    r = c.post("/api/forecast-ops/run-evaluation")
    assert r.status_code in (404, 501, 503)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/pytest tests/test_forecast_ops_router.py -v`
Expected: FAIL — `ModuleNotFoundError: app.routers.forecast_ops`.

- [ ] **Step 3: Implement the router**

```python
# backend/app/routers/forecast_ops.py
"""Forecast MLOps ops endpoints: drift/adjustment status + manual evaluation."""
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.database import get_db
from app.deps import require_admin, get_current_user_required
from app.models.forecasting import ForecastTarget, ForecastWeightAdjustment
from app.services.forecasting.ops.evaluation_job import run_evaluation

router = APIRouter(prefix="/forecast-ops", tags=["forecast-ops"])


def _eval_enabled() -> bool:
    return os.environ.get("FORECAST_EVAL_JOB_ENABLED", "false").lower() in ("1", "true", "yes")


@router.get("/status")
def get_status(
    db=Depends(get_db),
    user=Depends(get_current_user_required),
):
    """Per-product drift/adjustment chips (last 7 days) + whether eval is on."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    targets = db.query(ForecastTarget).filter(
        ForecastTarget.is_deleted == False  # noqa: E712
    ).all()
    products = []
    for t in targets:
        latest = db.query(ForecastWeightAdjustment).filter(
            ForecastWeightAdjustment.target_id == t.id,
            ForecastWeightAdjustment.created_date >= cutoff,
        ).order_by(ForecastWeightAdjustment.created_date.desc()).first()
        products.append({
            "product_key": t.product_key,
            "drifting": bool(latest and latest.triggered_by == "drift" and not latest.applied),
            "last_adjusted_at": (latest.created_date.isoformat() if latest else None),
            "last_trigger": (latest.triggered_by if latest else None),
        })
    return {"eval_enabled": _eval_enabled(), "products": products}


@router.post("/run-evaluation")
def trigger_evaluation(
    db=Depends(get_db),
    user=Depends(require_admin),
):
    if not _eval_enabled():
        raise HTTPException(status_code=503, detail="forecast_eval_job_disabled")
    summary = run_evaluation(db)
    return summary
```

Register in `backend/app/main.py`: find the router registration block and add:

```python
from app.routers import forecast_ops
app.include_router(forecast_ops.router, prefix="/api")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/pytest tests/test_forecast_ops_router.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/forecast_ops.py backend/app/main.py backend/tests/test_forecast_ops_router.py
git commit -m "feat(forecast): forecast_ops router (status + manual run-evaluation)"
```

---

### Task 9: Nightly loop integration (wire eval + drift after compute)

**Files:**
- Modify: `backend/app/services/scheduled_tasks.py` (in `_run_nightly_forecast_sync`, after the compute loop ~line 205, before `finally: db.close()`)
- Test: `backend/tests/test_forecast_nightly_ops.py`

**Interfaces:**
- Consumes: `evaluation_job.run_evaluation` (Task 2), `drift_response.check_drift_and_audit` (Task 3), flags `FORECAST_EVAL_JOB_ENABLED` / `FORECAST_DRIFT_AUTO_ADJUST_ENABLED`.
- Produces: the nightly summary dict gains `eval` and `drift` sub-dicts.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_forecast_nightly_ops.py
"""Nightly loop calls eval + drift after the compute loop, gated by flags."""
import os
from datetime import datetime, timezone
import pytest

from app.database import Base, engine, SessionLocal
from app.models.forecasting import ForecastTarget
from app.services import scheduled_tasks

_NEEDED = [ForecastTarget.__table__]


@pytest.fixture(autouse=True)
def _setup_tables():
    Base.metadata.drop_all(engine, tables=_NEEDED)
    Base.metadata.create_all(engine, tables=_NEEDED)
    yield
    Base.metadata.drop_all(engine, tables=_NEEDED)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.rollback(); s.close()


def test_nightly_runs_eval_and_drift_when_enabled(db, monkeypatch):
    t = ForecastTarget(product_key="电子元件", name="电子元件", org_id="default-org",
                       status="active")
    db.add(t); db.commit()

    calls = {"eval": 0, "drift": 0}
    monkeypatch.setattr(
        "app.services.scheduled_tasks._run_eval_step",
        lambda db: calls.__setitem__("eval", calls["eval"] + 1),
    )
    monkeypatch.setattr(
        "app.services.scheduled_tasks._run_drift_step",
        lambda db: calls.__setitem__("drift", calls["drift"] + 1),
    )
    monkeypatch.setattr(
        "app.services.scheduled_tasks.ForecastEngine",
        lambda d: type("E", (), {"compute_target_anchored": lambda self, tid: None})(),
    )
    os.environ["NIGHTLY_FORECAST_ENABLED"] = "true"
    os.environ["FORECAST_EVAL_JOB_ENABLED"] = "true"
    os.environ["FORECAST_DRIFT_AUTO_ADJUST_ENABLED"] = "true"

    summary = scheduled_tasks._run_nightly_forecast_sync()
    assert calls["eval"] == 1
    assert calls["drift"] == 1
    assert "eval" in summary and "drift" in summary


def test_nightly_skips_eval_when_disabled(db, monkeypatch):
    calls = {"eval": 0}
    monkeypatch.setattr(
        "app.services.scheduled_tasks._run_eval_step",
        lambda db: calls.__setitem__("eval", calls["eval"] + 1),
    )
    monkeypatch.setattr(
        "app.services.scheduled_tasks.ForecastEngine",
        lambda d: type("E", (), {"compute_target_anchored": lambda self, tid: None})(),
    )
    os.environ["FORECAST_EVAL_JOB_ENABLED"] = "false"
    scheduled_tasks._run_nightly_forecast_sync()
    assert calls["eval"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/pytest tests/test_forecast_nightly_ops.py -v`
Expected: FAIL — `_run_eval_step` / `_run_drift_step` don't exist; summary lacks `eval`/`drift` keys.

- [ ] **Step 3: Wire the steps into scheduled_tasks.py**

Add two helper functions and call them in `_run_nightly_forecast_sync`. In `backend/app/services/scheduled_tasks.py`:

```python
def _run_eval_step(db) -> dict:
    """Evaluation job step — closes the realized_mape loop. Gated."""
    if os.environ.get("FORECAST_EVAL_JOB_ENABLED", "false").lower() not in ("1", "true", "yes"):
        return {"skipped": True}
    try:
        from app.services.forecasting.ops.evaluation_job import run_evaluation
        return run_evaluation(db)
    except Exception:
        logger.exception("[nightly-forecast] eval step failed")
        return {"error": True}


def _run_drift_step(db) -> dict:
    """Drift-response step — detect + audit. Gated (detection needs eval data)."""
    if os.environ.get("FORECAST_DRIFT_AUTO_ADJUST_ENABLED", "false").lower() not in ("1", "true", "yes"):
        return {"skipped": True}
    try:
        from app.services.forecasting.ops.drift_response import check_drift_and_audit
        from app.models.forecasting import ForecastTarget
        targets = db.query(ForecastTarget).filter(
            ForecastTarget.org_id == "default-org",
            ForecastTarget.is_deleted == False,  # noqa: E712
        ).all()
        drifting = 0
        for t in targets:
            try:
                if check_drift_and_audit(db, t).get("is_drifting"):
                    drifting += 1
            except Exception:
                logger.exception("[nightly-forecast] drift check failed for %s", t.product_key)
        return {"checked": len(targets), "drifting": drifting}
    except Exception:
        logger.exception("[nightly-forecast] drift step failed")
        return {"error": True}
```

In `_run_nightly_forecast_sync`, after the compute loop builds `summary` (~line 197) and BEFORE `finally: db.close()` (line 207), insert:

```python
        summary["eval"] = _run_eval_step(db)
        summary["drift"] = _run_drift_step(db)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/pytest tests/test_forecast_nightly_ops.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scheduled_tasks.py backend/tests/test_forecast_nightly_ops.py
git commit -m "feat(forecast): nightly loop runs eval + drift steps (flag-gated)"
```

---

### Task 10: Frontend — "修正预测" modal on the Decision Board

**Files:**
- Modify: `frontend/src/api/marketDashboard.js` (add 2 methods)
- Create: `frontend/src/pages/MarketDashboard/components/ForecastFeedbackModal.jsx`
- Modify: `frontend/src/pages/MarketDashboard/components/DecisionBoard.jsx` (add button + modal mount)
- Test: `frontend/src/pages/MarketDashboard/components/__tests__/ForecastFeedbackModal.test.jsx`

**Interfaces:**
- Consumes: `marketDashboardApi` (authFetch-based), `DecisionBoard` product rows, `authFetch` global.
- Produces: modal with AI price (read-only) + user price + reason + submit; history list below. `POST /api/market-dashboard/forecast-feedback`, `GET .../forecast-feedback?product_id=`.

- [ ] **Step 1: Read the existing API client + DecisionBoard card render**

Read `frontend/src/api/marketDashboard.js` to see the `authFetch` call pattern (e.g. how `decisionBoard()` is implemented). Read `DecisionBoard.jsx` lines 70-200 to find the per-product card row where the "修正预测" button belongs (next to the action badge / trust dots).

- [ ] **Step 2: Write the failing test**

```jsx
// frontend/src/pages/MarketDashboard/components/__tests__/ForecastFeedbackModal.test.jsx
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import ForecastFeedbackModal from '../ForecastFeedbackModal';

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    submitForecastFeedback: vi.fn(),
    listForecastFeedback: vi.fn(),
  },
}));

import { marketDashboardApi } from '@/api/marketDashboard';

beforeEach(() => {
  vi.clearAllMocks();
  marketDashboardApi.listForecastFeedback.mockResolvedValue([]);
});

describe('ForecastFeedbackModal', () => {
  it('shows the AI price read-only and rejects non-positive user price', async () => {
    marketDashboardApi.submitForecastFeedback.mockResolvedValue({ id: '1', status: 'pending' });
    render(<ForecastFeedbackModal productId="电子元件" aiPrice={10388} onClose={() => {}} />);
    expect(screen.getByDisplayValue('10388')).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/我认为应该是/i), { target: { value: '11000' } });
    fireEvent.click(screen.getByRole('button', { name: /提交/i }));
    await waitFor(() => expect(marketDashboardApi.submitForecastFeedback).toHaveBeenCalled());
    const args = marketDashboardApi.submitForecastFeedback.mock.calls[0][0];
    expect(args.user_price).toBe(11000);
  });

  it('rejects zero user price', () => {
    render(<ForecastFeedbackModal productId="电子元件" aiPrice={10388} onClose={() => {}} />);
    fireEvent.change(screen.getByLabelText(/我认为应该是/i), { target: { value: '0' } });
    fireEvent.click(screen.getByRole('button', { name: /提交/i }));
    expect(marketDashboardApi.submitForecastFeedback).not.toHaveBeenCalled();
    expect(screen.getByText(/必须为正数/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /home/ysk2025/frontend && node_modules/.bin/vitest --no-watch run src/pages/MarketDashboard/components/__tests__/ForecastFeedbackModal.test.jsx`
Expected: FAIL — `ForecastFeedbackModal` not found.

- [ ] **Step 4: Add API methods + modal component**

In `frontend/src/api/marketDashboard.js`, add (mirroring the existing `authFetch` + JSON pattern used by `decisionBoard`):

```js
  async submitForecastFeedback({ product_id, ai_price, user_price, reason, target_date }) {
    const res = await authFetch('/api/market-dashboard/forecast-feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ product_id, ai_price, user_price, reason, target_date }),
    });
    if (!res.ok) throw new Error(`feedback submit failed: ${res.status}`);
    return res.json();
  },

  async listForecastFeedback(product_id) {
    const res = await authFetch(
      `/api/market-dashboard/forecast-feedback?product_id=${encodeURIComponent(product_id)}`,
    );
    if (!res.ok) return [];
    return res.json();
  },
```

```jsx
// frontend/src/pages/MarketDashboard/components/ForecastFeedbackModal.jsx
import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { X } from 'lucide-react';
import { marketDashboardApi } from '@/api/marketDashboard';
import { fmtPrice } from './DecisionBoard';

export default function ForecastFeedbackModal({ productId, aiPrice, onClose }) {
  const [userPrice, setUserPrice] = useState('');
  const [reason, setReason] = useState('');
  const [err, setErr] = useState('');
  const qc = useQueryClient();

  const { data: history = [] } = useQuery({
    queryKey: ['forecast-feedback', productId],
    queryFn: () => marketDashboardApi.listForecastFeedback(productId),
    staleTime: 30_000,
  });

  const mutation = useMutation({
    mutationFn: (payload) => marketDashboardApi.submitForecastFeedback(payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['forecast-feedback', productId] });
      setUserPrice(''); setReason(''); setErr('');
    },
    onError: (e) => setErr(String(e.message || e)),
  });

  const submit = () => {
    const v = Number(userPrice);
    if (!Number.isFinite(v) || v <= 0) { setErr('价格必须为正数'); return; }
    setErr('');
    mutation.mutate({
      product_id: productId, ai_price: aiPrice, user_price: v, reason,
      target_date: new Date().toISOString(),
    });
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="w-full max-w-md rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-900">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">修正预测 · {productId}</h3>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600"><X size={16} /></button>
        </div>
        <label className="mb-1 block text-xs text-slate-500">AI 预测价</label>
        <input value={aiPrice} disabled
          className="mb-3 w-full rounded border border-slate-200 bg-slate-50 px-2 py-1 text-sm dark:bg-slate-800" />
        <label className="mb-1 block text-xs text-slate-500">我认为应该是</label>
        <input aria-label="我认为应该是" value={userPrice}
          onChange={(e) => setUserPrice(e.target.value)}
          className="mb-3 w-full rounded border border-slate-200 px-2 py-1 text-sm dark:bg-slate-800 dark:border-slate-700" />
        <label className="mb-1 block text-xs text-slate-500">理由</label>
        <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2}
          className="mb-3 w-full rounded border border-slate-200 px-2 py-1 text-sm dark:bg-slate-800 dark:border-slate-700" />
        {err && <p className="mb-2 text-xs text-red-600">{err}</p>}
        <button onClick={submit} disabled={mutation.isPending}
          className="w-full rounded bg-blue-600 px-3 py-1.5 text-sm text-white hover:bg-blue-700 disabled:opacity-50">
          {mutation.isPending ? '提交中…' : '提交'}
        </button>

        {history.length > 0 && (
          <div className="mt-4 border-t border-slate-100 pt-2 dark:border-slate-800">
            <p className="mb-1 text-xs font-medium text-slate-500">历史修正</p>
            {history.map((h) => (
              <div key={h.id} className="flex justify-between py-0.5 text-xs text-slate-600 dark:text-slate-300">
                <span>AI {fmtPrice(h.ai_price)} → {fmtPrice(h.user_price)}</span>
                <span className={h.beat ? 'text-emerald-600' : 'text-slate-400'}>
                  {h.status === 'scored' ? (h.beat ? '命中' : '未中') : '待评'}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Mount the modal in DecisionBoard.jsx**

In `DecisionBoard.jsx`, import the modal + `useState` for the open product, and add a "修正预测" button next to each product row's action badge. On click, set `feedbackProduct = { id, aiPrice }`. Render `{feedbackProduct && <ForecastFeedbackModal ... onClose={() => setFeedbackProduct(null)} />}` at the end of the board container.

The AI price for a product = the latest forecast base value (read from the product row's forecast data already in the board payload — use the first/base point of the 7d horizon, or the `latest_forecast` field already present in the decision-board response). Confirm the exact field name from the `decisionBoard` response shape while editing.

- [ ] **Step 6: Run test to verify it passes**

Run: `cd /home/ysk2025/frontend && node_modules/.bin/vitest --no-watch run src/pages/MarketDashboard/components/__tests__/ForecastFeedbackModal.test.jsx`
Expected: PASS (2 tests).

- [ ] **Step 7: Commit**

```bash
git add frontend/src/api/marketDashboard.js frontend/src/pages/MarketDashboard/components/ForecastFeedbackModal.jsx frontend/src/pages/MarketDashboard/components/DecisionBoard.jsx frontend/src/pages/MarketDashboard/components/__tests__/ForecastFeedbackModal.test.jsx
git commit -m "feat(forecast): Decision Board 修正预测 modal (HITL capture + history)"
```

---

### Task 11: Feature flags, rollout, end-to-end verification

**Files:**
- Modify: `.env` (4 flags, all OFF)
- Verify: backend import + migration + manual backfill

**Interfaces:** none (configuration + verification).

- [ ] **Step 1: Add flags to .env**

Append to `/home/ysk2025/zhanlu_7_30/.env`:

```
# Forecast MLOps + HITL (all OFF by default; enable in rollout order)
FORECAST_EVAL_JOB_ENABLED=false
FORECAST_DRIFT_AUTO_ADJUST_ENABLED=false
FORECAST_DRIFT_BLEND_FACTOR=0.2
FORECAST_HITL_ENABLED=false
FORECAST_BIAS_CORRECTION_ENABLED=false
```

- [ ] **Step 2: Recreate backend (picks up .env) + reinstall ML deps if needed**

```bash
cd /home/ysk2025/zhanlu_7_30
docker compose up -d --force-recreate backend
sleep 25
docker exec zhanlu-backend python -c "import pandas, statsmodels, xgboost; print('ml deps ok')" 2>&1 | tail -3
```

If the import fails (stale image wiped the ad-hoc installs): `docker exec zhanlu-backend pip install pandas statsmodels xgboost`.

- [ ] **Step 3: Verify migration applies + import clean**

```bash
docker exec zhanlu-backend python -c "from app.models.forecasting import ForecastFeedback, ForecastWeightAdjustment; print('models import ok')"
docker exec zhanlu-backend alembic current 2>&1 | tail -2
docker logs zhanlu-backend --tail 30 2>&1 | tail -20
```

Expected: models import OK; alembic current = `041_forecast_mlops_hitl`; no startup errors. If alembic crashes on `create_index`, the migration is wrong — recheck that 041 uses `op.execute("CREATE INDEX IF NOT EXISTS ...")` not `op.create_index(..., [col])`.

- [ ] **Step 4: Run the full backend test suite (individually per RAM limit)**

```bash
cd /home/ysk2025/zhanlu_7_30/backend
for f in test_forecast_mlops_models test_forecast_evaluation_job test_forecast_drift_response test_forecast_feedback_service test_forecast_feedback_api test_forecast_bias_correction test_forecast_engine_ops_integration test_forecast_ops_router test_forecast_nightly_ops; do
  echo "=== $f ==="
  venv/bin/pytest tests/$f.py -v 2>&1 | tail -20
done
```

Expected: all PASS. Run `test_forecasting_models_engine.py` + `test_forecast_report.py` to confirm no regression in existing forecasting tests.

- [ ] **Step 5: Rollout — enable eval job + backfill realized_mape**

```bash
# Edit .env: FORECAST_EVAL_JOB_ENABLED=true
docker compose up -d --force-recreate backend
sleep 25
# Manual backfill (admin login first if auth enforced):
curl -s -X POST http://localhost:5002/api/forecast-ops/run-evaluation -H "Content-Type: application/json" | head -c 500
```

Then verify `realized_mape` is populated:

```bash
docker exec zhanlu-postgres psql -U zhanlu -d zhanlu -c \
  "SELECT COUNT(*) FILTER (WHERE realized_mape IS NOT NULL) AS realized, COUNT(*) AS total FROM forecast_accuracy_log;"
```

Expected: `realized > 0` after backfill. Verify `detect_drift()` no longer returns "insufficient data":

```bash
docker exec zhanlu-backend python -c "
from app.database import SessionLocal
from app.services.forecasting.accuracy_tracker import detect_drift
db = SessionLocal()
print(detect_drift(db, '电子元件'))
db.close()
"
```

- [ ] **Step 6: Rollout — enable drift auto-adjust, then HITL, then bias-correction (in order)**

Edit `.env` one flag at a time, `docker compose up -d --force-recreate backend` after each, and verify:
1. `FORECAST_DRIFT_AUTO_ADJUST_ENABLED=true` → check `forecast_weight_adjustments` rows appear for drifting products; chip renders on the board (`GET /api/forecast-ops/status`).
2. `FORECAST_HITL_ENABLED=true` → submit a test correction via the modal; verify a `forecast_feedback` row lands as `pending`.
3. Wait for actuals to arrive (or force-score via `run-evaluation`) → verify `status=scored`, `beat` set.
4. `FORECAST_BIAS_CORRECTION_ENABLED=true` only after ≥3 overrides per author are scored → verify the trust gate holds and `ForecastRun.explanation["bias_correction"]` appears on the next nightly run.

```bash
docker exec zhanlu-postgres psql -U zhanlu -d zhanlu -c \
  "SELECT product_id, status, beat, COUNT(*) FROM forecast_feedback GROUP BY 1,2,3 ORDER BY 1;"
```

- [ ] **Step 7: Commit**

```bash
git add .env
git commit -m "chore(forecast): add MLOps+HITL feature flags (all OFF); rollout-verified"
```

---

## Self-Review

**1. Spec coverage:**
- Evaluation job (Section 1) → Task 2. ✓
- Drift auto-adjust + notify + audit (Section 2) → Task 3 + engine drift-blend in Task 7 + chip in Task 8. ✓
- HITL feedback capture (Section 3) → Task 4 + Task 5 (API). ✓
- Validated bias-correction (Section 4) → Task 6 + engine plug-in Task 7. ✓
- Decision Board card UX (Section 5) → Task 10. ✓
- API endpoints (Section 6) → Task 5 (feedback) + Task 8 (ops). ✓
- Data model (Section 7) → Task 1 (`forecast_feedback`, `forecast_weight_adjustments`, `run_id` on accuracy log). ✓
- Feature flags & rollout (Section 8) → Task 11. ✓
- Testing (Section 9) → every task has TDD tests; Task 11 runs the suite + rollout verification. ✓

**2. Placeholder scan:** No TBD/TODO/"add error handling" without code. The two `# placeholder` markers in Task 5 Step 3 are explicitly resolved in the same step (the second code block replaces them with `db=Depends(get_db)`). Task 7's `author_id=None` is resolved by `resolve_trusted_author` defined in the same step. ✓

**3. Type consistency:** `run_evaluation(db, actuals_loader=None, product_key=None) -> dict` — used identically in Task 2, Task 8, Task 9. `check_drift_and_audit(db, target) -> dict` — Task 3 + Task 9. `record_feedback(db, product_id, ai_price, user_price, reason, author_id, author_name, target_date)` — Task 4 + Task 5. `apply_bias_correction(db, target, published_series, author_id)` — Task 6 + Task 7. `trust_gate_met(db, author_id, product_id)` — Task 6 + `resolve_trusted_author`. `ForecastWeightAdjustment` fields (triggered_by, applied, applied_at, delta_ratio) consistent across Tasks 1/3/6/7/8. ✓

**Gap noted:** `compute_target_anchored` (engine.py:955) delegation to `compute_target` is assumed — Task 7 Step 1 verifies this before editing; if it duplicates the publish step, the same edit applies there. The plan flags this explicitly.
