"""Agent-level risk-tier policy enforcement (IBM best practice, G3).

The agent catalog stores ``policy_profile`` per AgentApp (risk_tier,
rate_limit_per_minute). This module turns that metadata into RUNTIME
enforcement:

- **Tier-escalated gating**: agents with ``risk_tier`` high/critical
  require human confirmation for write/destructive tool calls even when
  the tool itself is rated lower. (System meta-agents keep their
  existing bypass — the orchestrators' consent is the UI click that
  started the conversation.)
- **Per-agent tool rate limit**: when ``rate_limit_per_minute`` is set,
  tool executions beyond the budget are denied until the sliding window
  frees up (in-process sliding window; per-worker, best-effort).

Resolution is cached on the request ``context`` dict — one DB read per
turn, not per tool call.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ESCALATION_TIERS = {"high", "critical"}
_RATE_BUCKETS: dict[str, deque] = {}


def resolve_agent_tier_policy(db, context: Optional[dict]) -> tuple[Optional[str], Optional[int]]:
    """(risk_tier, rate_limit_per_minute) for the context's agent, or
    ``(None, None)``. Cached on the context dict. Never raises."""
    if not context:
        return None, None
    cached = context.get("_tier_policy")
    if cached is not None:
        return cached
    try:
        from app.models.agent_app import AgentApp

        row = None
        app_id = context.get("agent_app_id")
        if app_id:
            row = db.query(AgentApp).filter(AgentApp.id == app_id).first()
        if row is None:
            name = context.get("agent_name")
            if name:
                row = (
                    db.query(AgentApp)
                    .filter(AgentApp.name == name, AgentApp.is_deleted == False)  # noqa: E712
                    .first()
                )
        if row is None:
            context["_tier_policy"] = (None, None)
            return None, None
        profile = getattr(row, "policy_profile", None)
        if not isinstance(profile, dict):
            context["_tier_policy"] = (None, None)
            return None, None
        tier = profile.get("risk_tier") or None
        rate = profile.get("rate_limit_per_minute")
        rate = int(rate) if isinstance(rate, (int, float)) and rate > 0 else None
        result = (tier, rate)
        context["_tier_policy"] = result
        return result
    except Exception as exc:  # noqa: BLE001 — policy must never break tools
        logger.debug("agent_tier: resolve failed: %s", exc)
        context["_tier_policy"] = (None, None)
        return None, None


def check_agent_rate_limit(agent_key: str, per_minute: int) -> tuple[bool, float]:
    """Sliding-window rate check. Returns ``(allowed, retry_after_s)``."""
    now = time.monotonic()
    bucket = _RATE_BUCKETS.setdefault(agent_key, deque())
    while bucket and bucket[0] <= now - 60.0:
        bucket.popleft()
    if len(bucket) >= per_minute:
        retry_after = max(0.5, 60.0 - (now - bucket[0]))
        return False, retry_after
    bucket.append(now)
    return True, 0.0


def escalation_applies(tier: Optional[str]) -> bool:
    return bool(tier) and tier in _ESCALATION_TIERS


__all__ = [
    "resolve_agent_tier_policy",
    "check_agent_rate_limit",
    "escalation_applies",
]
