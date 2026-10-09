"""Turn-health monitor — detect empty-bubble / fallback-collapse turns.

Root-fix monitoring layer (2026-10-09): the empty-bubble collapse
(memory-loop-guard + fake tool-free pass, conv 2e34ab0f) reached the USER
before anyone saw it in the logs. This scheduled scan reads recent
assistant turns and flags the generic fallback / apology texts so the
failure class surfaces as a CRITICAL log signal (and any alerting built
on logs) instead of a user report.

Pure DB scan — no LLM calls, cheap enough to run every 6h.

Flag: ``TURN_HEALTH_MONITOR_ENABLED`` (default True).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# The generic empty-bubble fallback text plus the apology family (both
# indicate the turn ended without a real answer).
_LITERAL_MARKERS = (
    "had trouble putting it all together",
    "try again with a more specific request",
)


def scan_recent_turns(
    db,
    *,
    hours: int = 24,
    min_turns: int = 5,
    threshold: float = 0.15,
) -> dict:
    """Scan assistant turns from the last ``hours`` for fallback texts.

    Returns ``{total, flagged, ratio, threshold_breached, examples}``.
    Never raises.
    """
    try:
        from app.models.chat_message import ChatMessage
        from app.services.agent_loop.fallbacks import _APOLOGY_PATTERN_RE

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        rows = (
            db.query(ChatMessage)
            .filter(
                ChatMessage.role == "assistant",
                ChatMessage.is_deleted == False,  # noqa: E712
                ChatMessage.created_date >= cutoff,
            )
            .order_by(ChatMessage.created_date.desc())
            .limit(2000)
            .all()
        )
        total = len(rows)
        flagged = []
        for r in rows:
            content = r.content or ""
            if not content.strip():
                continue  # truly-empty bubbles are handled elsewhere
            if "had trouble putting it all together" in content.lower() or any(
                m in content.lower() for m in _LITERAL_MARKERS
            ) or _APOLOGY_PATTERN_RE.search(content):
                flagged.append(r)

        ratio = (len(flagged) / total) if total else 0.0
        breached = bool(total >= min_turns and ratio >= threshold)
        summary = {
            "window_hours": hours,
            "total": total,
            "flagged": len(flagged),
            "ratio": round(ratio, 4),
            "threshold": threshold,
            "threshold_breached": breached,
            "examples": [
                {"id": r.id, "session_id": r.session_id,
                 "excerpt": (r.content or "")[:120]}
                for r in flagged[:3]
            ],
        }
        if breached:
            logger.critical(
                "TURN HEALTH: %d/%d assistant turns in the last %dh ended "
                "in a fallback/apology text (%.0f%% >= %.0f%% threshold). "
                "This is the empty-bubble collapse symptom — check "
                "'Tool-call loop guard tripped' / 'empty-bubble guarantee' "
                "log lines around the affected conversations. Examples: %s",
                len(flagged), total, hours, ratio * 100, threshold * 100,
                summary["examples"],
            )
        elif flagged:
            logger.warning(
                "TURN HEALTH: %d/%d fallback-flagged turns (below the "
                "%.0f%% threshold) — keep an eye on it",
                len(flagged), total, threshold * 100,
            )
        return summary
    except Exception as exc:  # noqa: BLE001 — monitoring never breaks anything
        logger.warning("turn_health_monitor: scan failed (non-fatal): %s", exc)
        return {"error": str(exc)}


async def monitor_cycle() -> dict:
    """Scheduled wrapper — no-op unless TURN_HEALTH_MONITOR_ENABLED."""
    from app.config import settings

    if not getattr(settings, "TURN_HEALTH_MONITOR_ENABLED", True):
        return {"enabled": False}
    try:
        import asyncio

        from app.database import SessionLocal

        def _run() -> dict:
            db = SessionLocal()
            try:
                return scan_recent_turns(db)
            finally:
                db.close()

        return await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001
        logger.warning("turn_health_monitor: cycle failed: %s", exc)
        return {"error": str(exc)}


__all__ = ["scan_recent_turns", "monitor_cycle"]
