"""User alert preferences — typed snapshot of the UserSetting alert_* columns.

Pure module: no DB writes, no I/O.  The router layer loads a ``UserSetting``
row and calls :meth:`AlertPrefSnapshot.from_user` once; the evaluator and
channel router then read only from the frozen snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


def safe_get(setting: Any, key: str, default: Any = None) -> Any:
    """Attribute-read a UserSetting column without crashing on missing/malformed data.

    Tolerates: ``setting is None``, missing attribute, ``None`` value, and a
    value whose container type mismatches the caller's expectation (e.g. a
    corrupted string where a JSON list belongs).
    """
    if setting is None:
        return default
    value = getattr(setting, key, None)
    if value is None:
        return default
    # Type-safety: list/dict defaults demand the same container type, otherwise
    # downstream code would iterate over a string / crash on a scalar.
    if isinstance(default, (list, dict)) and not isinstance(value, type(default)):
        return default
    return value


@dataclass(frozen=True)
class AlertPrefSnapshot:
    """Immutable per-user alerting preferences (safe defaults everywhere)."""

    user_id: str
    min_severity: str = "low"
    muted_products: list = field(default_factory=list)
    channels: list = field(default_factory=lambda: ["in_app"])
    threshold_multiplier: float = 1.0
    quiet_hours: Optional[dict] = None
    recommendations_enabled: bool = True
    webhook_url: Optional[str] = None
    webhook_secret: Optional[str] = None

    @classmethod
    def from_user(cls, user: Any, setting: Any) -> "AlertPrefSnapshot":
        """Build a snapshot from a User row + optional UserSetting row."""
        return cls(
            user_id=getattr(user, "id", "") or "",
            min_severity=str(safe_get(setting, "alert_min_severity", default="low") or "low"),
            muted_products=list(safe_get(setting, "alert_muted_products", default=[]) or []),
            channels=list(safe_get(setting, "alert_channels", default=["in_app"]) or ["in_app"]),
            threshold_multiplier=float(
                safe_get(setting, "alert_threshold_multiplier", default=1.0) or 1.0
            ),
            quiet_hours=safe_get(setting, "alert_quiet_hours", default=None),
            recommendations_enabled=bool(
                safe_get(setting, "alert_recommendations_enabled", default=True)
            ),
            webhook_url=safe_get(setting, "webhook_url", default=None),
            webhook_secret=safe_get(setting, "alert_webhook_secret", default=None),
        )

    # ── decision helpers ────────────────────────────────────────────────────

    def is_muted(self, product_id: str) -> bool:
        return product_id in (self.muted_products or [])

    def meets_min_severity(self, severity: str) -> bool:
        """True when ``severity`` is at or above the user's noise floor."""
        return SEVERITY_ORDER.get(severity, 0) >= SEVERITY_ORDER.get(self.min_severity, 0)

    def webhook_enabled(self) -> bool:
        return bool(self.webhook_url and self.webhook_secret)
