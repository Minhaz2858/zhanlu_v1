"""Analytics router — stores analytics tracking events in the database."""

import uuid
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_required
from app.models.analytics_event import AnalyticsEvent
from app.models.user import User

router = APIRouter(tags=["analytics"])


@router.post("/apps/{app_id}/analytics/track/batch")
async def track_batch(
    app_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Accept a batch of analytics events and persist them to the database."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    events = body.get("events", body) if isinstance(body, dict) else body
    if not isinstance(events, list):
        events = [body] if body else []

    # Only stamp user_id for real User rows. AnonymousIdentity (returned by
    # get_current_user_required when no auth token is present) carries a
    # per-browser UUID that is not in the users table, so inserting it into
    # analytics_events.user_id would violate the FK constraint and return 500.
    real_user = user if (user and not getattr(user, "is_anonymous", False)) else None

    stored = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("type", event.get("event_type", "unknown"))
        payload = {k: v for k, v in event.items() if k not in ("type", "event_type")}

        record = AnalyticsEvent(
            event_type=event_type,
            user_id=real_user.id if real_user else None,
            payload=str(payload) if payload else None,
        )
        db.add(record)
        stored += 1

    db.commit()
    return {"tracked": True, "count": stored}


@router.post("/apps/{app_id}/analytics/track")
async def track_single(
    app_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Accept a single analytics tracking event and persist it to the database."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    if not isinstance(body, dict):
        body = {"raw": str(body)}

    # Only stamp user_id for real User rows (see track_batch for rationale).
    real_user = user if (user and not getattr(user, "is_anonymous", False)) else None

    event_type = body.get("type", body.get("event_type", "unknown"))
    payload = {k: v for k, v in body.items() if k not in ("type", "event_type")}

    record = AnalyticsEvent(
        event_type=event_type,
        user_id=real_user.id if real_user else None,
        payload=str(payload) if payload else None,
    )
    db.add(record)
    db.commit()

    return {"tracked": True}
