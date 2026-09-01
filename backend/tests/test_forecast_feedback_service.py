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
    t = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(t); db.commit()
    fb = record_feedback(db, product_id="异戊二烯", ai_price=10388.0,
                         user_price=11000.0, reason="supply tightening",
                         author_id="u1", author_name="analyst",
                         target_date=datetime(2026, 8, 10))
    assert fb.status == "pending"
    assert fb.beat is None


def test_list_feedback_returns_history(db):
    t = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(t); db.commit()
    record_feedback(db, "异戊二烯", 100.0, 109.0, "x", "u1", "a1", datetime(2026, 8, 1))
    rows = list_feedback(db, "异戊二烯")
    assert len(rows) == 1
    assert rows[0]["user_price"] == 109.0


def test_author_track_record(db):
    t = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(t); db.commit()
    # 3 scored: 2 beat, 1 miss
    for ai, usr in [(100, 109), (100, 109), (100, 90)]:
        fb = record_feedback(db, "异戊二烯", float(ai), float(usr), "r", "u1", "a1", datetime(2026, 8, 1))
        fb.status = "scored"; fb.beat = (usr == 109); fb.ai_error = 0.1; fb.user_error = 0.01
    db.commit()
    tr = author_track_record(db, author_id="u1", product_id="异戊二烯")
    assert tr["scored"] == 3
    assert tr["beat"] == 2
    assert abs(tr["beat_rate"] - 2 / 3) < 0.01


def test_rejects_non_positive_user_price(db):
    t = ForecastTarget(product_key="异戊二烯", name="异戊二烯", org_id="default-org")
    db.add(t); db.commit()
    with pytest.raises(ValueError):
        record_feedback(db, "异戊二烯", 100.0, -5.0, "r", "u1", "a1", datetime(2026, 8, 1))
