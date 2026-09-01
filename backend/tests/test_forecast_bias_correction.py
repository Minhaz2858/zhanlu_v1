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
        target_id=target.id, product_id="异戊二烯", ai_price=float(ai),
        user_price=float(usr), reason="r", author_id=author, author_name=author,
        target_date=datetime.now(timezone.utc) - timedelta(days=days_ago),
        status="scored", beat=beat, ai_error=0.1, user_error=0.05,
        org_id="default-org",
    )
    fb.created_date = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.add(fb)


def test_gate_not_met_below_3(db):
    t = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(t); db.commit()
    _add_scored(db, t, "u1", 5, 100, 109, True)
    _add_scored(db, t, "u1", 4, 100, 109, True)
    db.commit()
    assert bc.trust_gate_met(db, "u1", "异戊二烯") is False


def test_gate_not_met_low_beat_rate(db):
    t = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(t); db.commit()
    for beat in [True, False, False]:
        _add_scored(db, t, "u1", 5, 100, 109, beat)
    db.commit()
    # 1/3 beat -> <50%
    assert bc.trust_gate_met(db, "u1", "异戊二烯") is False


def test_gate_met(db):
    t = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(t); db.commit()
    for beat in [True, True, False]:
        _add_scored(db, t, "u1", 5, 100, 109, beat)
    db.commit()
    assert bc.trust_gate_met(db, "u1", "异戊二烯") is True


def test_bias_delta_clamped_to_10pct(db):
    t = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(t); db.commit()
    # user consistently 50% above AI -> delta_ratio 0.5 -> clamped to 0.10
    for _ in range(4):
        _add_scored(db, t, "u1", 3, 100, 150, True)
    db.commit()
    delta = bc.compute_bias_delta(db, "异戊二烯", "u1")
    assert delta["clamped"] is True
    assert math.isclose(delta["delta_ratio"], 0.10, abs_tol=1e-6)


def test_apply_returns_unadjusted_when_gate_not_met(db):
    t = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(t); db.commit()
    s = pd.Series([100.0, 101.0, 102.0])
    out, expl = bc.apply_bias_correction(db, t, s, author_id="u1")
    assert expl is None
    assert np.allclose(out.values, s.values)


def test_apply_adjusts_and_audits_when_gate_met(db):
    t = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(t); db.commit()
    for _ in range(4):
        _add_scored(db, t, "u1", 3, 100, 110, True)  # +10% consistently
    db.commit()
    s = pd.Series([100.0, 101.0, 102.0])
    out, expl = bc.apply_bias_correction(db, t, s, author_id="u1")
    assert expl is not None
    assert math.isclose(expl["delta_ratio"], 0.10, abs_tol=1e-6)
    # 100 -> 110, 101 -> 111.1, 102 -> 112.2
    assert math.isclose(out.iloc[0], 110.0, abs_tol=0.01)
    n = db.query(ForecastWeightAdjustment).filter(
        ForecastWeightAdjustment.target_id == t.id,
        ForecastWeightAdjustment.triggered_by == "bias_correction",
    ).count()
    assert n == 1
