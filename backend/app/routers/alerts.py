"""Alerts API — proactive market alert panel backend.

Routes:
  GET  /api/alerts           — list active alerts (non-dismissed)
  POST /api/alerts/read      — mark all unread as read
  POST /api/alerts/dismiss/{alert_id} — dismiss a single alert
  POST /api/alerts/scan      — trigger a manual scan cycle (admin)

All routes gated by ``ECISCO_BI_ALERTS_ENABLED`` (returns 403 when off).
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import get_db, get_current_user_required
from app.models.user import User
from app.services.perception.alert_service import AlertService
from app.services.perception.worker import run_alert_scan_cycle

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/alerts", tags=["alerts"])


# ── Gate ────────────────────────────────────────────────────────────────

def _require_alerts_enabled():
    if not settings.ECISCO_BI_ALERTS_ENABLED:
        raise HTTPException(status_code=403, detail="ECISCO_BI_ALERTS_ENABLED is off")


def effective_org_id(current_user: User | None) -> str:
    """Resolve the effective org id for a request.

    The ``User`` model has no ``org_id`` column, so this falls back to
    ``settings.DEFAULT_ORG_ID`` (the single-tenant default) and keeps the
    reader/writer consistent with the background scan worker.
    """
    return getattr(current_user, "org_id", None) or settings.DEFAULT_ORG_ID


# ── Pydantic schemas ────────────────────────────────────────────────────

class AlertOut(BaseModel):
    id: str
    org_id: str | None = None
    product_id: str | None = None
    product_name: str | None = None
    alert_type: str
    severity: str
    change_7d_pct: float | None = None
    price: float | None = None
    message: str | None = None
    payload: dict | None = None
    triggered_at: str | None = None
    read_at: str | None = None
    dismissed_at: str | None = None
    source: str = "perception"

    class Config:
        from_attributes = True

    @field_validator("triggered_at", "read_at", "dismissed_at", mode="before")
    @classmethod
    def _dt_to_iso(cls, v):
        """ORM columns are DateTime; expose as ISO-8601 strings to the frontend."""
        if isinstance(v, datetime):
            return v.isoformat()
        return v

    @field_validator("source", mode="before")
    @classmethod
    def _source_default(cls, v):
        # ORM column default only applies at flush; NULL in-memory / legacy rows
        # would otherwise fail the non-null ``str`` field.
        return v or "perception"


class AlertListResponse(BaseModel):
    alerts: list[AlertOut]
    unread_count: int


class ScanResponse(BaseModel):
    scanned_at: str | None = None
    new_alerts: int = 0
    total_scanned: int = 0
    error: str | None = None
    skipped: bool = False
    reachable: bool | None = None
    reason: str | None = None


class AlertDiagnosticsResponse(BaseModel):
    flags: dict
    warehouse: dict
    thresholds: dict
    effective_org_id: str
    products: list[dict]
    triggering_products: list[str]


# ── Routes ──────────────────────────────────────────────────────────────

@router.get("", response_model=AlertListResponse)
def list_alerts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """List active (non-dismissed) alerts, newest first."""
    _require_alerts_enabled()

    org_id = effective_org_id(current_user)
    alerts = AlertService.get_active_alerts(db, org_id=org_id)
    unread = AlertService.count_unread(db, org_id=org_id)

    return AlertListResponse(
        alerts=[AlertOut.model_validate(a) for a in alerts],
        unread_count=unread,
    )


@router.post("/read")
def mark_all_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """Mark all unread alerts as read (per-org)."""
    _require_alerts_enabled()

    org_id = effective_org_id(current_user)
    count = AlertService.mark_all_read(db, org_id=org_id)
    return {"marked_read": count}


@router.post("/dismiss/{alert_id}")
def dismiss_alert(
    alert_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """Dismiss a single alert by ID."""
    _require_alerts_enabled()

    org_id = effective_org_id(current_user)
    ok = AlertService.dismiss_alert(db, alert_id, org_id=org_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"dismissed": alert_id}


@router.post("/scan", response_model=ScanResponse)
def trigger_scan(
    current_user: User = Depends(get_current_user_required),
):
    """Manually trigger an alert scan cycle (synchronous — blocks briefly)."""
    _require_alerts_enabled()

    import asyncio

    result = asyncio.run(run_alert_scan_cycle())
    return ScanResponse(**result)


@router.get("/diagnostics", response_model=AlertDiagnosticsResponse)
def alert_diagnostics(
    current_user: User = Depends(get_current_user_required),
):
    """Diagnose *why* the alerts panel may show zero alerts.

    Reports flag state, warehouse reachability, configured thresholds, and a
    per-product probe (row count / latest price / 7-day change / would-trigger).

    Gated by auth only (NOT ``ECISCO_BI_ALERTS_ENABLED``) so it can report the
    flag-off state instead of hiding the reason behind a 403.
    """
    from app.services.perception.diagnostics import build_alert_diagnostics

    return AlertDiagnosticsResponse(**build_alert_diagnostics())


@router.post("/digest", response_model=AlertOut)
async def trigger_digest(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """Manually trigger a CEO morning digest build (admin/debug)."""
    _require_alerts_enabled()
    if not settings.ALERT_DIGEST_ENABLED:
        raise HTTPException(status_code=403, detail="ALERT_DIGEST_ENABLED is off")

    from app.services.perception.digest_service import DigestService

    org_id = effective_org_id(current_user)
    svc = DigestService(db=db, org_id=org_id)
    result = await svc.build_and_persist()
    if not result:
        raise HTTPException(status_code=404, detail="No alerts to digest (quiet day)")
    return AlertOut.model_validate(result)
