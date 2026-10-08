# Wave 0 — F2: Decision-ROI Backtest Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the infrastructure to answer the customer's real question — *"if I had followed your buy/sell/hold advice, would I have saved money?"* — by logging every decision with its inputs, replaying past decisions against realized prices, and computing a money-saved ROI metric. This both gives a customer-facing trust number for the AI Brief and enables threshold calibration (T1.4).

**Architecture:** A new `ForecastDecisionLog` table records each decision the engine emits (action, p_rise, expected_change, confidence, trust_tier, thresholds used, as-of date). A pure replay module `decision_roi.py` takes a sequence of logged decisions plus a realized-price series and computes per-decision and aggregate ROI by simulating a simple execution rule (buy = purchase at as-of price, sell/avoid at as-of price; compare against a hold baseline). The ROI summary service exposes aggregate stats for the brief. Reuses the existing `decision_engine.recommend()` and `accuracy_tracker` realized-price machinery.

**Tech Stack:** Python 3.11, SQLAlchemy 2.0 ORM, pandas, pytest (`backend/venv/bin/python -m pytest`).

## Global Constraints

- **Backend venv**: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest <path> -v`.
- **Schema management**: `Base.metadata.create_all()` at startup — no Alembic migration.
- **No future leakage**: ROI replay uses only prices `<=` the decision's as-of date + horizon. The realized price for a decision dated T is the actual price at T+horizon.
- **Decision semantics** (mirrors `decision_engine.py`): `buy` = "procure now", `sell` = "defer/avoid purchase", `hold`/`watch` = no action. ROI compares acting on the advice vs a naive "always buy at as-of" baseline.
- **ROI sign convention**: positive ROI = the advice saved money vs always-buying-blind. A `sell` decision that precedes a price drop saves the customer `(price_T - price_T+h)/price_T` × assumed volume.
- **Docker**: after touching `models/forecasting.py` → `docker restart zhanlu-backend`.
- **Decision thresholds** (current, from `decision_engine.py`): `_BUY_THRESHOLD=0.70`, `_SELL_THRESHOLD=0.30`, `_BUY_MIN_CHANGE=0.03`, `_SELL_MIN_CHANGE=-0.03`, `_EDGE_THRESHOLD=0.55`. These are recorded per-log so calibration (T1.4) can replay under alternative thresholds.

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `backend/app/models/forecasting.py` | Add `ForecastDecisionLog` ORM model | Modify (append) |
| `backend/app/models/__init__.py` | Export `ForecastDecisionLog` | Modify (append import) |
| `backend/app/services/forecasting/decision_roi.py` | Pure ROI replay + summary functions (no I/O) | Create |
| `backend/app/services/forecasting/decision_logger.py` | Thin wrapper that writes a `ForecastDecisionLog` row from a `Decision` + context | Create |
| `backend/app/services/forecasting/accuracy_tracker.py` | Wire realized-price evaluation to also score decision ROI (append `score_decision_roi`) | Modify (append function) |
| `backend/tests/test_forecasting/test_decision_log_model.py` | ORM round-trip test | Create |
| `backend/tests/test_forecasting/test_decision_roi.py` | Pure ROI replay tests | Create |
| `backend/tests/test_forecasting/test_decision_logger.py` | Logger write test | Create |

---

### Task 1: `ForecastDecisionLog` ORM model

**Files:**
- Modify: `backend/app/models/forecasting.py` (append after `ForecastEventImpact` / `ForecastWeightAdjustment`)
- Modify: `backend/app/models/__init__.py` (append import)
- Test: `backend/tests/test_forecasting/test_decision_log_model.py`

**Interfaces:**
- Produces: `ForecastDecisionLog(TimestampedBase)` — table `forecast_decision_logs`:
  - `target_id` (FK → forecast_targets.id, indexed, nullable)
  - `product_key` (String(100), indexed)
  - `as_of_date` (DateTime, indexed) — the decision date T
  - `horizon_days` (Integer) — h
  - `action` (String(20)) — buy/hold/sell/watch
  - `confidence` (String(20)) — high/medium/low
  - `p_rise` (Float, nullable)
  - `expected_change_pct` (Float, nullable)
  - `directional_acc` (Float, nullable)
  - `trust_tier` (String(20), nullable)
  - `thresholds` (JSON) — snapshot of decision_engine constants at decision time
  - `rationale` (Text, nullable)
  - `realized_price_t` (Float, nullable) — price at T (filled at log time)
  - `realized_price_th` (Float, nullable) — price at T+h (filled by scorer)
  - `decision_roi_pct` (Float, nullable) — filled by scorer
  - `scored_at` (DateTime, nullable, indexed)
  - `status` (String(20), default "pending") — pending/scored

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_forecasting/test_decision_log_model.py
"""Round-trip test for ForecastDecisionLog ORM model on SQLite."""
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.base import TimestampedBase
from app.models.forecasting import ForecastDecisionLog


def test_decision_log_round_trip():
    engine = create_engine("sqlite:///:memory:")
    TimestampedBase.metadata.create_all(engine, tables=[ForecastDecisionLog.__table__])
    with Session(engine) as session:
        log = ForecastDecisionLog(
            product_key="product_d",
            as_of_date=datetime(2026, 7, 1),
            horizon_days=7,
            action="buy",
            confidence="medium",
            p_rise=0.74,
            expected_change_pct=0.045,
            directional_acc=0.58,
            trust_tier="high",
            thresholds={"buy_p": 0.70, "sell_p": 0.30, "min_change": 0.03,
                        "edge_accuracy": 0.55},
            rationale="P(rise)=74%, expected +4.5%",
            realized_price_t=11000.0,
        )
        session.add(log)
        session.commit()
        loaded = session.query(ForecastDecisionLog).filter_by(
            product_key="product_d", as_of_date=datetime(2026, 7, 1)
        ).one()
        assert loaded.action == "buy"
        assert loaded.p_rise == 0.74
        assert loaded.status == "pending"
        assert loaded.thresholds["edge_accuracy"] == 0.55
        assert loaded.realized_price_th is None
        assert loaded.decision_roi_pct is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecasting/test_decision_log_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'ForecastDecisionLog'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/models/forecasting.py`:

```python
class ForecastDecisionLog(TimestampedBase):
    """Audit + ROI record for every buy/hold/sell decision the engine emits.

    Written at decision time (status=pending, realized_price_th/decision_roi_pct
    null), then scored by the realized-eval job once the horizon date's actual
    price arrives. Enables two things the customer cares about:
      1. A money-saved ROI number for the AI Brief (Wave 2 T1.4 calibration).
      2. Threshold grid-search: replay the same decisions under alternative
         thresholds to find the constants that maximize realized ROI.
    """

    __tablename__ = "forecast_decision_logs"

    target_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("forecast_targets.id"), nullable=True, index=True
    )
    product_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    as_of_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)

    action: Mapped[str] = mapped_column(String(20), nullable=False)
    confidence: Mapped[str] = mapped_column(String(20), nullable=False, default="low")
    p_rise: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expected_change_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    directional_acc: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trust_tier: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    thresholds: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    realized_price_t: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realized_price_th: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    decision_roi_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    scored_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
```

Append `ForecastDecisionLog` to the import in `backend/app/models/__init__.py` (run `grep -n "from app.models.forecasting" app/models/__init__.py` to find the line first).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecasting/test_decision_log_model.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/models/forecasting.py backend/app/models/__init__.py backend/tests/test_forecasting/test_decision_log_model.py
git commit -m "feat(forecast): add ForecastDecisionLog model for decision ROI tracking"
```

---

### Task 2: Pure decision-ROI replay module

**Files:**
- Create: `backend/app/services/forecasting/decision_roi.py`
- Test: `backend/tests/test_forecasting/test_decision_roi.py`

**Interfaces:**
- Produces:
  - `score_decision(action, realized_price_t, realized_price_th) -> dict` — single-decision ROI. For `buy`: roi = (price_th - price_t)/price_t (you bought at T, value at T+h). For `sell`/`watch`: roi = (price_t - price_th)/price_t (you deferred, saved the drop). `hold`: roi = 0. Returns `{"roi_pct": float|None, "correct_direction": bool, "note": str}`.
  - `aggregate_roi(scored_logs: list[dict]) -> dict` — `{"n": int, "n_actionable": int, "mean_roi_pct": float, "n_correct_direction": int, "total_saved_pct": float}`. `scored_logs` items have `action`, `decision_roi_pct`, `correct_direction`.
  - `replay_under_thresholds(logs, realized_prices, buy_p, sell_p, min_change, edge_acc) -> dict` — re-derives each decision's action under alternative thresholds (for T1.4 calibration) using the logged `p_rise`/`expected_change_pct`/`directional_acc`, then scores against the same realized prices. Returns the same aggregate shape.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_forecasting/test_decision_roi.py
"""Tests for pure decision-ROI replay functions."""
import pytest

from app.services.forecasting.decision_roi import (
    score_decision, aggregate_roi, replay_under_thresholds,
)


class TestScoreDecision:
    def test_buy_when_price_rises_positive_roi(self):
        r = score_decision("buy", 10000.0, 10500.0)
        assert r["roi_pct"] == pytest.approx(0.05)
        assert r["correct_direction"] is True

    def test_buy_when_price_falls_negative_roi(self):
        r = score_decision("buy", 10000.0, 9500.0)
        assert r["roi_pct"] == pytest.approx(-0.05)
        assert r["correct_direction"] is False

    def test_sell_defer_saves_when_price_drops(self):
        # sell = defer purchase; price fell 5% -> you saved 5%
        r = score_decision("sell", 10000.0, 9500.0)
        assert r["roi_pct"] == pytest.approx(0.05)
        assert r["correct_direction"] is True

    def test_sell_defer_loses_when_price_rises(self):
        r = score_decision("sell", 10000.0, 10500.0)
        assert r["roi_pct"] == pytest.approx(-0.05)
        assert r["correct_direction"] is False

    def test_hold_zero_roi(self):
        r = score_decision("hold", 10000.0, 10500.0)
        assert r["roi_pct"] == 0.0

    def test_watch_zero_roi(self):
        r = score_decision("watch", 10000.0, 10500.0)
        assert r["roi_pct"] == 0.0

    def test_none_when_price_missing(self):
        r = score_decision("buy", 10000.0, None)
        assert r["roi_pct"] is None


class TestAggregateRoi:
    def test_aggregate_basic(self):
        logs = [
            {"action": "buy", "decision_roi_pct": 0.05, "correct_direction": True},
            {"action": "sell", "decision_roi_pct": 0.03, "correct_direction": True},
            {"action": "hold", "decision_roi_pct": 0.0, "correct_direction": True},
            {"action": "buy", "decision_roi_pct": -0.02, "correct_direction": False},
        ]
        agg = aggregate_roi(logs)
        assert agg["n"] == 4
        assert agg["n_actionable"] == 3  # buy+sell+buy, hold excluded
        assert agg["mean_roi_pct"] == pytest.approx(0.02, abs=1e-4)  # (0.05+0.03-0.02)/3
        assert agg["n_correct_direction"] == 3

    def test_aggregate_empty(self):
        agg = aggregate_roi([])
        assert agg["n"] == 0
        assert agg["mean_roi_pct"] is None


class TestReplayUnderThresholds:
    def test_looser_threshold_produces_more_actions(self):
        # logged decisions with p_rise just below default buy threshold
        logs = [
            {"p_rise": 0.65, "expected_change_pct": 0.04,
             "directional_acc": 0.58, "trust_tier": "high",
             "realized_price_t": 10000.0, "realized_price_th": 10400.0,
             "as_of_date": "2026-07-01"},
        ]
        # Default threshold 0.70 -> action would be "hold" (0.65 < 0.70)
        strict = replay_under_thresholds(logs, buy_p=0.70, sell_p=0.30,
                                          min_change=0.03, edge_acc=0.55)
        assert strict["n_actionable"] == 0
        # Looser threshold 0.60 -> action "buy" -> ROI realized
        loose = replay_under_thresholds(logs, buy_p=0.60, sell_p=0.30,
                                         min_change=0.03, edge_acc=0.55)
        assert loose["n_actionable"] == 1
        assert loose["mean_roi_pct"] == pytest.approx(0.04, abs=1e-4)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecasting/test_decision_roi.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.forecasting.decision_roi'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/forecasting/decision_roi.py
"""Pure decision-ROI replay + aggregation (no I/O).

Convention: positive ROI = the advice saved/made money vs always-buying-blind.
  buy   -> procure at T;        roi = (px_th - px_t) / px_t
  sell  -> defer purchase at T; roi = (px_t  - px_th) / px_t   (you avoided the move)
  hold  -> no action;           roi = 0
  watch -> no action;           roi = 0
"""
from __future__ import annotations

from datetime import datetime


def score_decision(action: str, realized_price_t, realized_price_th) -> dict:
    """Score a single decision against realized prices."""
    if realized_price_t is None or realized_price_th is None or realized_price_t == 0:
        return {"roi_pct": None, "correct_direction": False,
                "note": "missing realized price"}
    a = (action or "watch").lower()
    if a == "buy":
        roi = (realized_price_th - realized_price_t) / realized_price_t
        correct = realized_price_th > realized_price_t
    elif a == "sell":
        roi = (realized_price_t - realized_price_th) / realized_price_t
        correct = realized_price_th < realized_price_t
    else:  # hold / watch
        roi = 0.0
        correct = True
    return {"roi_pct": round(roi, 6), "correct_direction": correct, "note": ""}


def aggregate_roi(scored_logs: list[dict]) -> dict:
    """Aggregate ROI across scored decision logs.

    Each item: {action, decision_roi_pct, correct_direction}. Actionable =
    buy or sell (hold/watch excluded from mean since roi is always 0).
    """
    n = len(scored_logs)
    if n == 0:
        return {"n": 0, "n_actionable": 0, "mean_roi_pct": None,
                "n_correct_direction": 0, "total_saved_pct": None}
    actionable = [l for l in scored_logs
                  if (l.get("action") or "").lower() in ("buy", "sell")
                  and l.get("decision_roi_pct") is not None]
    rois = [l["decision_roi_pct"] for l in actionable]
    mean_roi = sum(rois) / len(rois) if rois else None
    n_correct = sum(1 for l in scored_logs if l.get("correct_direction"))
    return {
        "n": n,
        "n_actionable": len(actionable),
        "mean_roi_pct": round(mean_roi, 6) if mean_roi is not None else None,
        "n_correct_direction": n_correct,
        "total_saved_pct": round(sum(rois), 6) if rois else None,
    }


def _derive_action(p_rise, expected_change_pct, directional_acc, trust_tier,
                   buy_p, sell_p, min_change, edge_acc) -> str:
    """Mirror decision_engine logic under alternative thresholds."""
    tier = (trust_tier or "low").lower()
    has_edge = (
        directional_acc is not None
        and directional_acc >= edge_acc
    )
    if not has_edge or tier == "low":
        return "watch"
    if p_rise is not None and expected_change_pct is not None:
        if p_rise >= buy_p and expected_change_pct > min_change:
            return "buy"
        if p_rise <= sell_p and expected_change_pct < -min_change:
            return "sell"
    return "hold"


def replay_under_thresholds(logs, buy_p, sell_p, min_change, edge_acc) -> dict:
    """Re-derive + re-score decisions under alternative thresholds.

    Each log item: {p_rise, expected_change_pct, directional_acc, trust_tier,
                    realized_price_t, realized_price_th}.
    """
    scored = []
    for l in logs:
        action = _derive_action(
            l.get("p_rise"), l.get("expected_change_pct"),
            l.get("directional_acc"), l.get("trust_tier"),
            buy_p, sell_p, min_change, edge_acc,
        )
        s = score_decision(action, l.get("realized_price_t"),
                           l.get("realized_price_th"))
        scored.append({"action": action, "decision_roi_pct": s["roi_pct"],
                       "correct_direction": s["correct_direction"]})
    return aggregate_roi(scored)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecasting/test_decision_roi.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/services/forecasting/decision_roi.py backend/tests/test_forecasting/test_decision_roi.py
git commit -m "feat(forecast): add pure decision_roi replay + aggregation module"
```

---

### Task 3: Decision logger wrapper

**Files:**
- Create: `backend/app/services/forecasting/decision_logger.py`
- Test: `backend/tests/test_forecasting/test_decision_logger.py`

**Interfaces:**
- Consumes: a `Decision` dataclass (from `decision_engine`), target/product context, horizon, realized price at T.
- Produces: `log_decision(db_session, *, target_id, product_key, as_of_date, horizon_days, decision, p_rise, expected_change_pct, directional_acc, trust_tier, realized_price_t) -> ForecastDecisionLog` — writes and returns a pending log row.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_forecasting/test_decision_logger.py
"""Test decision logger writes a pending ForecastDecisionLog row."""
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.base import TimestampedBase
from app.models.forecasting import ForecastDecisionLog
from app.services.forecasting.decision_engine import Decision
from app.services.forecasting.decision_logger import log_decision


def test_log_decision_writes_pending_row():
    engine = create_engine("sqlite:///:memory:")
    TimestampedBase.metadata.create_all(engine, tables=[ForecastDecisionLog.__table__])
    with Session(engine) as session:
        dec = Decision("buy", "medium", "P(rise)=74%, expected +4.5%")
        log = log_decision(
            session,
            target_id=None,
            product_key="product_d",
            as_of_date=datetime(2026, 7, 1),
            horizon_days=7,
            decision=dec,
            p_rise=0.74,
            expected_change_pct=0.045,
            directional_acc=0.58,
            trust_tier="high",
            realized_price_t=11000.0,
        )
        session.commit()
        assert log.action == "buy"
        assert log.confidence == "medium"
        assert log.p_rise == 0.74
        assert log.status == "pending"
        assert log.realized_price_t == 11000.0
        assert log.realized_price_th is None
        assert log.thresholds["buy_p"] == 0.70
        # Row persisted
        loaded = session.query(ForecastDecisionLog).filter_by(
            product_key="product_d").one()
        assert loaded.action == "buy"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecasting/test_decision_logger.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.forecasting.decision_logger'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/forecasting/decision_logger.py
"""Thin wrapper that writes a ForecastDecisionLog row from a Decision."""
from __future__ import annotations

from datetime import datetime

from app.models.forecasting import ForecastDecisionLog
from app.services.forecasting.decision_engine import Decision

# Snapshot of decision_engine thresholds at log time (for T1.4 replay)
_THRESHOLD_SNAPSHOT = {
    "buy_p": 0.70, "sell_p": 0.30, "min_change": 0.03,
    "edge_accuracy": 0.55, "p_high_margin": 0.25,
}


def log_decision(
    db_session,
    *,
    target_id: str | None,
    product_key: str,
    as_of_date: datetime,
    horizon_days: int,
    decision: Decision,
    p_rise: float | None,
    expected_change_pct: float | None,
    directional_acc: float | None,
    trust_tier: str | None,
    realized_price_t: float | None,
) -> ForecastDecisionLog:
    """Write a pending decision log row and return it.

    Caller is responsible for ``session.commit()``.
    """
    log = ForecastDecisionLog(
        target_id=target_id,
        product_key=product_key,
        as_of_date=as_of_date,
        horizon_days=horizon_days,
        action=decision.action,
        confidence=decision.confidence,
        p_rise=p_rise,
        expected_change_pct=expected_change_pct,
        directional_acc=directional_acc,
        trust_tier=trust_tier,
        thresholds=dict(_THRESHOLD_SNAPSHOT),
        rationale=decision.rationale,
        realized_price_t=realized_price_t,
        status="pending",
    )
    db_session.add(log)
    return log
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecasting/test_decision_logger.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/services/forecasting/decision_logger.py backend/tests/test_forecasting/test_decision_logger.py
git commit -m "feat(forecast): add decision_logger to persist ForecastDecisionLog rows"
```

---

### Task 4: Realized-eval scorer — fills `realized_price_th` + `decision_roi_pct`

**Files:**
- Modify: `backend/app/services/forecasting/accuracy_tracker.py` (append `score_pending_decisions`)
- Test: `backend/tests/test_forecasting/test_decision_roi.py` (append `TestScorePendingDecisions`)

**Interfaces:**
- Produces: `score_pending_decisions(db, realized_price_lookup) -> dict` — finds all `ForecastDecisionLog` rows where `status="pending"` and `as_of_date + horizon_days <= today`, fills `realized_price_th` from `realized_price_lookup[(product_key, date)]`, computes `decision_roi_pct` via `decision_roi.score_decision`, sets `status="scored"` + `scored_at`. Returns aggregate via `aggregate_roi`. `realized_price_lookup` is a callable `(product_key, date) -> float | None` or a dict.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_forecasting/test_decision_roi.py`:

```python
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.base import TimestampedBase
from app.models.forecasting import ForecastDecisionLog
from app.services.forecasting.accuracy_tracker import score_pending_decisions


class TestScorePendingDecisions:
    def test_scores_pending_logs(self):
        engine = create_engine("sqlite:///:memory:")
        TimestampedBase.metadata.create_all(engine, tables=[ForecastDecisionLog.__table__])
        as_of = datetime(2026, 7, 1)
        horizon = 7
        with Session(engine) as session:
            session.add(ForecastDecisionLog(
                product_key="product_d", as_of_date=as_of, horizon_days=horizon,
                action="buy", confidence="medium", p_rise=0.74,
                expected_change_pct=0.045, realized_price_t=10000.0,
                status="pending",
            ))
            session.commit()

        # Realized price at T+7 = 10400 (buy was correct, +4%)
        def lookup(product_key, date):
            if product_key == "product_d" and date == as_of + timedelta(days=7):
                return 10400.0
            return None

        with Session(engine) as session:
            agg = score_pending_decisions(session, lookup)
            session.commit()

        assert agg["n"] == 1
        assert agg["n_actionable"] == 1
        assert agg["mean_roi_pct"] == pytest.approx(0.04, abs=1e-4)

        with Session(engine) as session:
            log = session.query(ForecastDecisionLog).one()
            assert log.status == "scored"
            assert log.realized_price_th == 10400.0
            assert log.decision_roi_pct == pytest.approx(0.04, abs=1e-4)
            assert log.scored_at is not None

    def test_skips_logs_without_realized_price(self):
        engine = create_engine("sqlite:///:memory:")
        TimestampedBase.metadata.create_all(engine, tables=[ForecastDecisionLog.__table__])
        as_of = datetime(2026, 7, 1)
        with Session(engine) as session:
            session.add(ForecastDecisionLog(
                product_key="product_d", as_of_date=as_of, horizon_days=7,
                action="buy", confidence="medium", realized_price_t=10000.0,
                status="pending",
            ))
            session.commit()

        with Session(engine) as session:
            agg = score_pending_decisions(session, lambda pk, d: None)
            session.commit()

        assert agg["n"] == 0  # nothing scored
        with Session(engine) as session:
            log = session.query(ForecastDecisionLog).one()
            assert log.status == "pending"  # still pending
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecasting/test_decision_roi.py::TestScorePendingDecisions -v`
Expected: FAIL — `ImportError: cannot import name 'score_pending_decisions'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/forecasting/accuracy_tracker.py`:

```python
# ---------------------------------------------------------------------------
# Decision ROI scoring (Wave 0 F2)
# ---------------------------------------------------------------------------

def score_pending_decisions(db, realized_price_lookup) -> dict:
    """Score pending ForecastDecisionLog rows whose horizon has elapsed.

    ``realized_price_lookup`` is a callable ``(product_key, date) -> float|None``
    or a dict keyed by ``(product_key, date)``.
    """
    from datetime import datetime, timedelta

    from app.models.forecasting import ForecastDecisionLog
    from app.services.forecasting.decision_roi import score_decision, aggregate_roi

    def _lookup(product_key, date):
        if callable(realized_price_lookup):
            return realized_price_lookup(product_key, date)
        return realized_price_lookup.get((product_key, date))

    now = datetime.utcnow()
    pending = db.query(ForecastDecisionLog).filter(
        ForecastDecisionLog.status == "pending",
    ).all()

    scored = []
    for log in pending:
        target_date = log.as_of_date + timedelta(days=log.horizon_days)
        if target_date > now:
            continue  # horizon not yet elapsed
        px_th = _lookup(log.product_key, target_date)
        if px_th is None:
            continue  # realized price not yet available
        result = score_decision(log.action, log.realized_price_t, px_th)
        log.realized_price_th = float(px_th)
        log.decision_roi_pct = result["roi_pct"]
        log.scored_at = now
        log.status = "scored"
        scored.append({"action": log.action,
                       "decision_roi_pct": result["roi_pct"],
                       "correct_direction": result["correct_direction"]})
    return aggregate_roi(scored)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecasting/test_decision_roi.py -v`
Expected: PASS (all, including TestScorePendingDecisions)

- [ ] **Step 5: Run full forecasting test suite for regressions**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_forecasting/ -v`
Expected: all PASS

- [ ] **Step 6: Restart backend to register the new table**

Run: `docker restart zhanlu-backend`

- [ ] **Step 7: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30
git add backend/app/services/forecasting/accuracy_tracker.py backend/tests/test_forecasting/test_decision_roi.py
git commit -m "feat(forecast): add score_pending_decisions to fill decision ROI from realized prices"
```

---

## Self-Review

**1. Spec coverage:**
- Decision-ROI backtest harness (replay past decisions vs realized) → Task 2 (`decision_roi.py`) + Task 4 (scorer) ✓
- `ForecastDecisionLog` model (decision + inputs + realized outcome) → Task 1 ✓
- Threshold grid-search replay (for T1.4 calibration) → Task 2 `replay_under_thresholds` ✓
- Wire ROI into accuracy tracker → Task 4 ✓
- Customer-facing "money saved" number → `aggregate_roi` produces `total_saved_pct` / `mean_roi_pct` (brief consumption is T1.4) ✓
- Engine integration (auto-logging each decision) → **deferred**: the logger (Task 3) is ready; wiring it into the nightly compute job's decision-emission path is a small follow-up in T1.4 to avoid changing the compute pipeline in Wave 0. ✓

**2. Placeholder scan:** No TBD/TODO. All steps have actual code.

**3. Type consistency:**
- `Decision` dataclass fields (`action`, `confidence`, `rationale`) match `log_decision` usage ✓
- `score_decision` returns `{roi_pct, correct_direction, note}` — consumed by `aggregate_roi` items `{action, decision_roi_pct, correct_direction}` and by the scorer ✓
- `ForecastDecisionLog` fields match test assertions and the scorer's writes (`realized_price_th`, `decision_roi_pct`, `scored_at`, `status`) ✓
- `_THRESHOLD_SNAPSHOT` keys (`buy_p`, `sell_p`, `min_change`, `edge_accuracy`) match `replay_under_thresholds` params ✓
