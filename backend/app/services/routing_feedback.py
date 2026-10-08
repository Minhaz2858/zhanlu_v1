"""Routing feedback reader — closes the IBM continuous-optimization loop.

Reads the per-turn ``route``/``outcome``/``duration_ms`` records that E1
enriched into ``agent_invocations`` and computes advisory adjustments for
the routing ladder (FSM-vs-ReAct classifier threshold). Read-only advisor:
thresholds change only when ``ROUTING_FEEDBACK_ENABLED`` is on AND the
stats are significant (``min_turns`` within the window); otherwise the
caller gets ``None`` and keeps the hand-tuned default.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_THRESHOLD_FLOOR = 0.3
_THRESHOLD_CEIL = 0.9
_STEP = 0.05


@dataclass
class RouteStats:
    route: str
    turns: int
    success_rate: float
    p50_ms: float
    p95_ms: float


def compute_route_stats(
    db: Session,
    *,
    window_hours: int = 168,
    min_turns: int = 20,
) -> list[RouteStats]:
    """Aggregate per-route success rate + latency over the window."""
    from collections import defaultdict
    from datetime import datetime, timedelta, timezone

    from app.models.agent_invocation import AgentInvocation

    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    rows = (
        db.query(AgentInvocation.route, AgentInvocation.status, AgentInvocation.duration_ms)
        .filter(
            AgentInvocation.route.isnot(None),
            AgentInvocation.created_date >= since,
        )
        .all()
    )

    by_route: dict[str, dict] = defaultdict(lambda: {"turns": 0, "ok": 0, "lat": []})
    for row in rows:
        if isinstance(row, (tuple, list)):
            route, status, duration_ms = row
        else:  # mapped-object rows (defensive; real query returns tuples)
            route, status, duration_ms = row.route, row.status, row.duration_ms
        bucket = by_route[route]
        bucket["turns"] += 1
        if status == "completed":
            bucket["ok"] += 1
        if duration_ms is not None:
            bucket["lat"].append(duration_ms)

    stats: list[RouteStats] = []
    for route, bucket in by_route.items():
        if bucket["turns"] < min_turns:
            continue
        lat = sorted(bucket["lat"])
        stats.append(RouteStats(
            route=route,
            turns=bucket["turns"],
            success_rate=bucket["ok"] / bucket["turns"],
            p50_ms=_percentile(lat, 0.5),
            p95_ms=_percentile(lat, 0.95),
        ))
    return stats


def _percentile(sorted_vals: list[int], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return float(sorted_vals[idx])


def _stats_by_route(stats: list[RouteStats]) -> dict[str, RouteStats]:
    return {s.route: s for s in stats}


def suggest_fsm_threshold(
    db: Session,
    *,
    current_threshold: float = 0.5,
    window_hours: int = 168,
    min_turns: int = 20,
) -> float | None:
    """Adjusted FSM-routing classifier threshold, or None if data is thin.

    - FSM clearly better (success >= react + 0.05 AND fsm p95 <= react p95)
      → lower the threshold by 0.05 (route more turns to FSM).
    - FSM worse (success < react) → raise by 0.05.
    - Result clamped to [0.3, 0.9]. Never returns a value equal to the
      input when data is inconclusive — returns None instead.
    """
    stats = _stats_by_route(compute_route_stats(
        db, window_hours=window_hours, min_turns=min_turns))
    fsm, react = stats.get("fsm"), stats.get("react")
    if not fsm or not react:
        return None

    if fsm.success_rate >= react.success_rate + 0.05 and fsm.p95_ms <= react.p95_ms:
        suggested = current_threshold - _STEP
    elif fsm.success_rate < react.success_rate:
        suggested = current_threshold + _STEP
    else:
        return None

    return max(_THRESHOLD_FLOOR, min(_THRESHOLD_CEIL, suggested))


# ── Consumption seam (cached 5 min) ──────────────────────────────────────

_CACHE: dict = {"overrides": None, "expires": 0.0}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_S = 300.0


def get_suggested_overrides(db: Session, *, force: bool = False) -> dict | None:
    """Overrides for the routing ladder, cached 5 minutes.

    Returns None when ``ROUTING_FEEDBACK_ENABLED`` is off (log-only mode)
    or the data is inconclusive.
    """
    from app.config import settings

    if not getattr(settings, "ROUTING_FEEDBACK_ENABLED", False):
        return None
    now = time.monotonic()
    with _CACHE_LOCK:
        if not force and _CACHE["overrides"] is not None and now < _CACHE["expires"]:
            return _CACHE["overrides"]
    try:
        from app.config import settings as _s

        suggested = suggest_fsm_threshold(db)
        overrides = None
        if suggested is not None:
            overrides = {"fsm_plan_confidence": suggested}
            logger.info(
                "routing_feedback: FSM threshold %.2f → %.2f "
                "(stats over window; set ROUTING_FEEDBACK_ENABLED=false to pin)",
                0.5, suggested,
            )
        with _CACHE_LOCK:
            _CACHE["overrides"] = overrides
            _CACHE["expires"] = now + _CACHE_TTL_S
        return overrides
    except Exception as exc:  # noqa: BLE001 — feedback must never break routing
        logger.debug("routing_feedback: override computation failed: %s", exc)
        return None
