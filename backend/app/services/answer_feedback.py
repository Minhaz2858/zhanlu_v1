"""Answer feedback service — aggregate the EXISTING feedback capture.

2026-10-13 consolidation: the chat UI already had the full loop —
MessageActions 👍/👎 → POST .../messages/{id}/feedback → ExperienceEntry
(entry_type="user_feedback", outcome positive/negative) → recipe/cache/
HITL learning. What was missing was the BUSINESS view: the ROI and
portfolio boards never read it. This module aggregates those durable
ExperienceEntry rows into per-agent acceptance rates.

The earlier AnswerFeedback table/endpoint from this session was removed
as a duplicate capture path (DRY): one capture, many consumers.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def feedback_summary(
    db: Session,
    org_id: str,
    days: int = 30,
    app_id: str | None = None,
) -> dict:
    """Per-agent acceptance rates from ExperienceEntry user_feedback rows."""
    from app.models.experience_entry import ExperienceEntry

    start = datetime.now(timezone.utc) - timedelta(days=max(1, min(int(days), 365)))
    q = (
        db.query(ExperienceEntry)
        .filter(
            ExperienceEntry.org_id == org_id,
            ExperienceEntry.entry_type == "user_feedback",
            ExperienceEntry.is_deleted == False,  # noqa: E712
            ExperienceEntry.created_date >= start,
        )
    )
    if app_id:
        q = q.filter(ExperienceEntry.app_id == app_id)

    per_agent: dict[str, dict[str, int]] = {}
    totals = {"up": 0, "down": 0}
    for row in q.all():
        outcome = (row.outcome or "").lower()
        if outcome in ("positive", "negative"):
            rating = 1 if outcome == "positive" else -1
        elif not outcome:
            # Outcome missing — fall back to the numeric rating.
            ur = row.user_rating or 0
            rating = 1 if ur > 0 else (-1 if ur < 0 else 0)
        else:
            continue  # unknown outcome label — don't guess
        if rating == 0:
            continue
        agent = row.agent_app_id or "unknown"
        slot = per_agent.setdefault(agent, {"up": 0, "down": 0})
        slot["up" if rating > 0 else "down"] += 1
        totals["up" if rating > 0 else "down"] += 1

    def _rate(slot: dict) -> float | None:
        total = slot["up"] + slot["down"]
        return round(slot["up"] / total, 3) if total else None

    return {
        "org_id": org_id,
        "window_days": days,
        "totals": {**totals, "acceptance_rate": _rate(totals)},
        "by_agent": {
            agent: {**slot, "acceptance_rate": _rate(slot)}
            for agent, slot in sorted(per_agent.items())
        },
    }
