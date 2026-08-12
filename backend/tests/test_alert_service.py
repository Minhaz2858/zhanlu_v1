"""Unit tests for AlertService — detection, dedup, CRUD, mark_all_read, dismiss.

Run:  pytest backend/tests/test_alert_service.py -v
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.alert import AlertLog


# ── In-memory SQLite test DB fixture ──────────────────────────────────

@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", echo=False)
    AlertLog.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()
    engine.dispose()


# ── Helpers ──────────────────────────────────────────────────────────

def _make_snapshot(products: list[dict]) -> list[dict]:
    """Build a perception snapshot fixture."""
    return products


def _alert_count(db) -> int:
    return db.query(AlertLog).count()


# ── Detection tests ──────────────────────────────────────────────────

def test_detect_price_spike(db_session):
    """7d change >= +3% → price_spike alert."""
    from app.services.perception.alert_service import AlertService

    snapshot = _make_snapshot([
        {
            "product_id": "P001", "product_name": "Polyethylene",
            "current_price": 100.0, "change_7d_pct": 5.0,
        },
    ])
    result = AlertService.detect_and_persist(db_session, "org-1", snapshot)
    db_session.commit()

    assert len(result) == 1
    alert = result[0]
    assert alert["alert_type"] == "price_spike"
    assert alert["severity"] == "medium"
    assert alert["product_id"] == "P001"
    assert "surged" in alert["message"]
    assert _alert_count(db_session) == 1


def test_detect_price_drop(db_session):
    """7d change <= -3% → price_drop alert."""
    from app.services.perception.alert_service import AlertService

    snapshot = _make_snapshot([
        {
            "product_id": "P002", "product_name": "PVC",
            "current_price": 50.0, "change_7d_pct": -4.5,
        },
    ])
    result = AlertService.detect_and_persist(db_session, "org-1", snapshot)
    db_session.commit()

    assert len(result) == 1
    assert result[0]["alert_type"] == "price_drop"
    assert result[0]["severity"] == "medium"
    assert "dropped" in result[0]["message"]


def test_detect_price_spike_high_severity(db_session):
    """≥6% spike → high severity."""
    from app.services.perception.alert_service import AlertService

    snapshot = _make_snapshot([
        {
            "product_id": "P003", "product_name": "Crude Oil",
            "current_price": 80.0, "change_7d_pct": 7.5,
        },
    ])
    result = AlertService.detect_and_persist(db_session, "org-1", snapshot)
    assert result[0]["severity"] == "high"


def test_detect_price_drop_high_severity(db_session):
    """≤-6% drop → high severity."""
    from app.services.perception.alert_service import AlertService

    snapshot = _make_snapshot([
        {
            "product_id": "P004", "product_name": "Naphtha",
            "current_price": 40.0, "change_7d_pct": -8.0,
        },
    ])
    result = AlertService.detect_and_persist(db_session, "org-1", snapshot)
    assert result[0]["severity"] == "high"


def test_normal_range_no_alert(db_session):
    """Change between -3% and +3% → no alert."""
    from app.services.perception.alert_service import AlertService

    snapshot = _make_snapshot([
        {
            "product_id": "P005", "product_name": "Normal Product",
            "current_price": 60.0, "change_7d_pct": 1.5,
        },
    ])
    result = AlertService.detect_and_persist(db_session, "org-1", snapshot)
    db_session.commit()

    assert len(result) == 0
    assert _alert_count(db_session) == 0


# ── Dedup tests ──────────────────────────────────────────────────────

def test_dedup_prevents_duplicate(db_session):
    """Same (product_id, alert_type) within 7 days → not re-inserted."""
    from app.services.perception.alert_service import AlertService

    snapshot = _make_snapshot([
        {
            "product_id": "P010", "product_name": "Ethylene",
            "current_price": 90.0, "change_7d_pct": 5.0,
        },
    ])
    # First scan
    r1 = AlertService.detect_and_persist(db_session, "org-1", snapshot)
    assert len(r1) == 1

    # Second scan — same product, same window
    r2 = AlertService.detect_and_persist(db_session, "org-1", snapshot)
    assert len(r2) == 0
    assert _alert_count(db_session) == 1


def test_dedup_allows_different_type(db_session):
    """Same product, different alert_type → allowed."""
    from app.services.perception.alert_service import AlertService

    # spike first
    AlertService.detect_and_persist(db_session, "org-1", _make_snapshot([
        {"product_id": "P011", "product_name": "X", "current_price": 100, "change_7d_pct": 5.0},
    ]))

    # drop second — different type
    r2 = AlertService.detect_and_persist(db_session, "org-1", _make_snapshot([
        {"product_id": "P011", "product_name": "X", "current_price": 50, "change_7d_pct": -5.0},
    ]))
    assert len(r2) == 1
    assert _alert_count(db_session) == 2


def test_recent_alert_exists(db_session):
    """recent_alert_exists() returns True when alert exists within window."""
    from app.services.perception.alert_service import AlertService

    db_session.add(AlertLog(
        id=str(uuid.uuid4()),
        org_id="org-1",
        product_id="P020",
        alert_type="price_spike",
        triggered_at=datetime.now(timezone.utc) - timedelta(days=2),
    ))
    db_session.commit()

    assert AlertService.recent_alert_exists(db_session, "P020", "price_spike", "org-1")
    assert not AlertService.recent_alert_exists(db_session, "P020", "price_drop", "org-1")
    assert not AlertService.recent_alert_exists(db_session, "P999", "price_spike", "org-1")


# ── CRUD tests ───────────────────────────────────────────────────────

def test_get_active_alerts(db_session):
    """get_active_alerts returns non-dismissed alerts, newest first."""
    from app.services.perception.alert_service import AlertService

    now = datetime.now(timezone.utc)
    a1 = AlertLog(
        id="a-1", org_id="org-1", product_id="P1",
        alert_type="price_spike", triggered_at=now - timedelta(hours=1),
    )
    a2 = AlertLog(
        id="a-2", org_id="org-1", product_id="P2",
        alert_type="price_drop", triggered_at=now,
    )
    a3 = AlertLog(
        id="a-3", org_id="org-1", product_id="P3",
        alert_type="price_spike", triggered_at=now - timedelta(hours=2),
        dismissed_at=now,  # dismissed — should NOT appear
    )
    db_session.add_all([a1, a2, a3])
    db_session.commit()

    alerts = AlertService.get_active_alerts(db_session, org_id="org-1")
    assert len(alerts) == 2
    # newest first → a2 then a1
    assert alerts[0].id == "a-2"
    assert alerts[1].id == "a-1"


def test_count_unread(db_session):
    """count_unread returns only unread + non-dismissed."""
    from app.services.perception.alert_service import AlertService

    now = datetime.now(timezone.utc)
    db_session.add_all([
        AlertLog(id="u1", org_id="org-1", product_id="P1", alert_type="price_spike",
                 triggered_at=now),
        AlertLog(id="u2", org_id="org-1", product_id="P2", alert_type="price_drop",
                 triggered_at=now, read_at=now),  # read → not counted
        AlertLog(id="u3", org_id="org-1", product_id="P3", alert_type="price_spike",
                 triggered_at=now, dismissed_at=now),  # dismissed → not counted
    ])
    db_session.commit()

    assert AlertService.count_unread(db_session, org_id="org-1") == 1


def test_mark_all_read(db_session):
    """mark_all_read sets read_at for all unread, non-dismissed alerts."""
    from app.services.perception.alert_service import AlertService

    now = datetime.now(timezone.utc)
    db_session.add_all([
        AlertLog(id="m1", org_id="org-1", product_id="P1", alert_type="price_spike",
                 triggered_at=now),
        AlertLog(id="m2", org_id="org-1", product_id="P2", alert_type="price_drop",
                 triggered_at=now, read_at=now),
    ])
    db_session.commit()

    count = AlertService.mark_all_read(db_session, org_id="org-1")
    assert count == 1

    # Now all should be read
    assert AlertService.count_unread(db_session, org_id="org-1") == 0


def test_dismiss_alert(db_session):
    """dismiss_alert sets dismissed_at and returns True."""
    from app.services.perception.alert_service import AlertService

    db_session.add(AlertLog(
        id="d1", org_id="org-1", product_id="P1",
        alert_type="price_spike",
        triggered_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    ok = AlertService.dismiss_alert(db_session, "d1", org_id="org-1")
    assert ok

    alert = db_session.query(AlertLog).filter(AlertLog.id == "d1").first()
    assert alert.dismissed_at is not None
    assert AlertService.get_active_alerts(db_session, org_id="org-1") == []


def test_dismiss_alert_not_found(db_session):
    """dismiss_alert for non-existent ID returns False."""
    from app.services.perception.alert_service import AlertService

    ok = AlertService.dismiss_alert(db_session, "nonexistent", org_id="org-1")
    assert not ok


# ── Arbitrage detection tests ────────────────────────────────────────

def test_detect_arbitrage(db_session):
    """Regional spread > freight cost → arbitrage alert."""
    from app.services.perception.alert_service import AlertService

    snapshot = [
        {
            "product_id": "C5-001", "product_name": "C5 Raffinate",
            "region": "East China", "current_price": 100.0,
            "freight_cost": 10.0, "change_7d_pct": 0.0,
        },
        {
            "product_id": "C5-001", "product_name": "C5 Raffinate",
            "region": "South China", "current_price": 130.0,
            "freight_cost": 10.0, "change_7d_pct": 0.0,
        },
    ]
    result = AlertService.detect_and_persist(db_session, "org-1", snapshot)
    db_session.commit()

    assert len(result) == 1
    assert result[0]["alert_type"] == "regional_arbitrage"
    assert result[0]["severity"] == "medium"
    assert int(result[0]["price"]) == 30  # spread
    assert _alert_count(db_session) == 1


def test_no_arbitrage_when_spread_below_freight(db_session):
    """spread <= freight → no arbitrage alert."""
    from app.services.perception.alert_service import AlertService

    snapshot = [
        {
            "product_id": "C5-002", "region": "East",
            "current_price": 100.0, "freight_cost": 30.0,
            "product_name": "C5-2", "change_7d_pct": 0.0,
        },
        {
            "product_id": "C5-002", "region": "South",
            "current_price": 115.0, "freight_cost": 30.0,
            "product_name": "C5-2", "change_7d_pct": 0.0,
        },
    ]
    result = AlertService.detect_and_persist(db_session, "org-1", snapshot)
    assert len(result) == 0


# ── Edge cases ───────────────────────────────────────────────────────

def test_empty_snapshot(db_session):
    """Empty snapshot → no alerts, no errors."""
    from app.services.perception.alert_service import AlertService

    result = AlertService.detect_and_persist(db_session, "org-1", [])
    assert result == []


def test_snapshot_missing_fields(db_session):
    """Missing product_id or change_7d_pct → skip gracefully."""
    from app.services.perception.alert_service import AlertService

    snapshot = [
        {"product_name": "No ID", "change_7d_pct": 5.0},  # no product_id
        {"product_id": "PXXX", "change_7d_pct": None},    # no change_7d
        {"product_id": "POK", "product_name": "Good", "current_price": 100, "change_7d_pct": 5.0},
    ]
    result = AlertService.detect_and_persist(db_session, "org-1", snapshot)
    assert len(result) == 1
    assert result[0]["product_id"] == "POK"


def test_org_isolation(db_session):
    """Alerts are scoped per org_id."""
    from app.services.perception.alert_service import AlertService

    # Insert alert for org-1
    AlertService.detect_and_persist(db_session, "org-1", _make_snapshot([
        {"product_id": "P1", "product_name": "X", "current_price": 100, "change_7d_pct": 5.0},
    ]))

    # org-2 should NOT see org-1's alerts
    assert len(AlertService.get_active_alerts(db_session, org_id="org-2")) == 0
    assert AlertService.count_unread(db_session, org_id="org-2") == 0

    # org-1 should see its own
    assert len(AlertService.get_active_alerts(db_session, org_id="org-1")) == 1
    assert AlertService.count_unread(db_session, org_id="org-1") == 1
