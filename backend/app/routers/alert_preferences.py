"""GET/PATCH /api/users/me/alert-preferences — per-user smart-alert tuning.

Reads/writes the ``alert_*`` columns on the user's ``UserSetting`` row.  A
missing row is lazily created on first access with safe defaults.  The webhook
secret is auto-generated the first time the user configures a webhook URL.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import asdict
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user_required
from app.models import UserSetting
from app.models.user import User
from app.services.alerts.preferences import AlertPrefSnapshot

logger = logging.getLogger(__name__)

router = APIRouter()

# API field name → UserSetting column name (only the settings we manage).
_COLUMN_MAP = {
    "min_severity": "alert_min_severity",
    "muted_products": "alert_muted_products",
    "channels": "alert_channels",
    "threshold_multiplier": "alert_threshold_multiplier",
    "quiet_hours": "alert_quiet_hours",
    "recommendations_enabled": "alert_recommendations_enabled",
    "webhook_url": "webhook_url",
}


class AlertPreferencesPatch(BaseModel):
    """Partial update payload; unknown fields rejected (422)."""

    model_config = {"extra": "forbid"}

    min_severity: Optional[Literal["low", "medium", "high"]] = None
    muted_products: Optional[list[str]] = None
    channels: Optional[list[Literal["in_app", "webhook"]]] = None
    threshold_multiplier: Optional[float] = Field(default=None, ge=0.5, le=2.0)
    quiet_hours: Optional[dict[str, Any]] = None
    recommendations_enabled: Optional[bool] = None
    webhook_url: Optional[str] = None

    @field_validator("webhook_url")
    @classmethod
    def _validate_webhook_url(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("webhook_url must be an http(s) URL")
        return v


def _load_or_create(db: Session, user: User) -> UserSetting:
    setting = (
        db.query(UserSetting)
        .filter(UserSetting.created_by_id == user.id)
        .filter(UserSetting.is_deleted == False)  # noqa: E712
        .first()
    )
    if setting is None:
        setting = UserSetting(created_by_id=user.id)
        db.add(setting)
        db.flush()
    return setting


def _snapshot_response(user: User, setting: UserSetting) -> dict:
    """API-shaped preference snapshot (field names == dataclass fields)."""
    return asdict(AlertPrefSnapshot.from_user(user, setting))


@router.get("/users/me/alert-preferences")
def get_alert_preferences(
    user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
) -> dict:
    setting = _load_or_create(db, user)
    db.commit()
    db.refresh(setting)
    return _snapshot_response(user, setting)


@router.patch("/users/me/alert-preferences")
def update_alert_preferences(
    patch: AlertPreferencesPatch,
    user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
) -> dict:
    setting = _load_or_create(db, user)
    data = patch.model_dump(exclude_unset=True)
    for key, value in data.items():
        if value is None:
            continue
        setattr(setting, _COLUMN_MAP.get(key, key), value)
    # Auto-generate the HMAC secret on first webhook configuration.
    if data.get("webhook_url") and not getattr(setting, "alert_webhook_secret", None):
        setting.alert_webhook_secret = secrets.token_urlsafe(32)
    db.commit()
    db.refresh(setting)
    return _snapshot_response(user, setting)
