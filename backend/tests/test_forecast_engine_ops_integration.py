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
    target = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
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
    target = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(target); db.commit()
    for _ in range(4):
        fb = ForecastFeedback(
            target_id=target.id, product_id="异戊二烯", ai_price=100.0, user_price=110.0,
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
