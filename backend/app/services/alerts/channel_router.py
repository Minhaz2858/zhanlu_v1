"""Channel dispatch for smart alerts — in-app + HMAC-signed webhook.

Webhook delivery is fire-and-forget with a single retry (5xx/timeout/connect).
4xx responses are treated as permanent (drop, no retry).  Quiet hours suppress
webhook delivery entirely but never block the in-app channel.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app.services.alerts.preferences import AlertPrefSnapshot

logger = logging.getLogger(__name__)

WEBHOOK_TIMEOUT_S = 5.0
WEBHOOK_RETRY_DELAY_S = 30.0
WEBHOOK_EVENT = "zhanlu.alert"


def _sign(body: bytes, secret: str) -> str:
    """HMAC-SHA256 signature header value for a webhook payload."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def build_webhook_payload(alert: dict) -> dict:
    """Flatten an enriched alert into the external webhook event shape."""
    inner = alert.get("payload") or {}
    impact = inner.get("impact") or {}
    return {
        "event": WEBHOOK_EVENT,
        "alert_type": alert.get("alert_type"),
        "severity": alert.get("severity"),
        "product_id": alert.get("product_id"),
        "product_name": alert.get("product_name"),
        "message": alert.get("message"),
        "smart": inner.get("smart"),
        "impact_yuan_per_day": impact.get("impact_yuan_per_day"),
    }


async def _post_once(
    client: httpx.AsyncClient, url: str, content: bytes, headers: dict
) -> Optional[int]:
    """POST once; return status code, or None when the call raised."""
    try:
        resp = await client.post(url=url, content=content, headers=headers)
        return resp.status_code
    except httpx.HTTPError as exc:
        logger.warning("webhook POST to %s failed: %s", url, exc)
        return None


def _succeeded(status: Optional[int]) -> bool:
    return status is not None and 200 <= status < 400


async def fire_webhook(
    url: str, payload: dict, secret: str, retry_delay: float = WEBHOOK_RETRY_DELAY_S
) -> bool:
    """Deliver a signed webhook with one retry on transient failure.

    Returns True when delivered (or a retryable error eventually recovered),
    False when permanently dropped (4xx) or both attempts failed.
    """
    if not url or not secret:
        return False
    body = json.dumps(payload, sort_keys=True).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Zhanlu-Signature": _sign(body, secret),
    }
    async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_S) as client:
        status = await _post_once(client, url, body, headers)
        if status is None or status >= 500:
            # Transient failure (raise or 5xx) → single retry after a backoff.
            await asyncio.sleep(retry_delay)
            status = await _post_once(client, url, body, headers)
            if not _succeeded(status):
                logger.warning("webhook to %s still failing after retry (status=%s)", url, status)
                return False
            return True
        if 400 <= status < 500:
            logger.warning("webhook 4xx from %s (status=%s), dropping payload", url, status)
            return False
        return _succeeded(status)


def _to_minutes(hhmm: str) -> Optional[int]:
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _in_quiet_hours(pref: AlertPrefSnapshot, now: Optional[datetime] = None) -> bool:
    """True when ``now`` falls inside the user's quiet-hours window.

    Supports overnight windows (start > end).  Malformed/disabled config and
    any parse failure degrade to "not in quiet hours" (never block delivery).
    """
    qh = pref.quiet_hours
    if not isinstance(qh, dict) or not qh.get("enabled"):
        return False
    start = qh.get("start")
    end = qh.get("end")
    start_min = _to_minutes(start) if isinstance(start, str) else None
    end_min = _to_minutes(end) if isinstance(end, str) else None
    if start_min is None or end_min is None:
        return False
    now = now or datetime.now(timezone.utc)
    current_min = now.hour * 60 + now.minute
    if start_min <= end_min:
        return start_min <= current_min < end_min
    return current_min >= start_min or current_min < end_min


async def dispatch(
    alert: dict, pref: AlertPrefSnapshot, now: Optional[datetime] = None
) -> list[str]:
    """Route an enriched alert to the user's enabled channels.

    Returns the list of channels actually used (always includes ``in_app``
    when enabled; ``webhook`` only when configured, outside quiet hours, and
    delivered successfully).
    """
    used: list[str] = []
    if "in_app" in (pref.channels or []):
        used.append("in_app")
    if "webhook" in (pref.channels or []) and pref.webhook_enabled():
        if _in_quiet_hours(pref, now):
            logger.info(
                "quiet hours active; skipping webhook for %s",
                alert.get("product_id"),
            )
        else:
            payload = build_webhook_payload(alert)
            ok = await fire_webhook(pref.webhook_url, payload, pref.webhook_secret)
            if ok:
                used.append("webhook")
            else:
                logger.warning(
                    "webhook delivery failed for %s", alert.get("product_id")
                )
    return used
