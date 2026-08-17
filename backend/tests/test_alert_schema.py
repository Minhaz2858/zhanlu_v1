"""Tests for AlertOut schema datetime handling.

Regression: GET /api/alerts returned 500 because AlertOut declared
``triggered_at`` / ``read_at`` / ``dismissed_at`` as ``str`` while the
``AlertLog`` ORM produces ``datetime`` objects. Pydantic v2 refuses
datetime -> str coercion, so ``model_validate`` raised ValidationError.

Run:  pytest backend/tests/test_alert_schema.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.models.alert import AlertLog
from app.routers.alerts import AlertOut


def test_alert_out_accepts_orm_datetimes():
    """AlertOut.model_validate must accept a real AlertLog ORM instance.

    The timestamps must come out as ISO-8601 strings (what the frontend
    passes to ``new Date(...)``).
    """
    alert = AlertLog(
        id="a1",
        org_id="org-1",
        product_id="P001",
        product_name="Polyethylene",
        alert_type="price_spike",
        severity="high",
        message="surged 5%",
        triggered_at=datetime(2026, 8, 17, 2, 30, 0, tzinfo=timezone.utc),
        read_at=None,
        dismissed_at=None,
        payload=None,
    )
    out = AlertOut.model_validate(alert)

    assert out.id == "a1"
    assert out.triggered_at == "2026-08-17T02:30:00+00:00"
    assert out.read_at is None
    assert out.dismissed_at is None


def test_alert_out_passes_through_iso_strings():
    """Pre-serialized iso strings (e.g. from to_dict()) must pass unchanged."""
    out = AlertOut.model_validate(
        {
            "id": "a2",
            "alert_type": "price_drop",
            "severity": "low",
            "triggered_at": "2026-08-17T02:30:00+00:00",
        }
    )
    assert out.triggered_at == "2026-08-17T02:30:00+00:00"
