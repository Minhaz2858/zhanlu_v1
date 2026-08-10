"""Async Event Registry — SQLAlchemy-backed persistence for intelligence events.

Ported from EDIA's ``event_registry.py`` which used raw MySQL/SQLite SQL.
Adapted to zhanlu's async SQLAlchemy ORM pattern with ``IntelligenceEvent``
and ``IntelligenceIngestionStatus`` models.

Key changes from EDIA:
- All methods are ``async``
- Uses SQLAlchemy ``select()``, ``update()`` instead of raw ``text()`` SQL
- Module-level singleton removed — callers pass a db session
- Uses ``IntelligenceEvent`` ORM model instead of raw dict rows
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from sqlalchemy import select, update, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.intelligence import IntelligenceEvent, IntelligenceIngestionStatus
from app.services.intelligence.event_types import ExtractedEvent

logger = logging.getLogger(__name__)


class IntelligenceEventRegistry:
    """Async SQLAlchemy registry for intelligence events and ingestion status."""

    def __init__(self, db: AsyncSession):
        self._db = db

    @staticmethod
    def _default_review_status() -> str:
        raw_value = (
            os.getenv("INTELLIGENCE_DEFAULT_REVIEW_STATUS", "approved") or "approved"
        ).strip().lower()
        if raw_value in {"pending", "approved", "rejected"}:
            return raw_value
        return "approved"

    @staticmethod
    def _compute_auto_approval(event: ExtractedEvent, desired_status: str) -> str:
        """Phase 5: Auto-approve events that meet quality thresholds.

        Rules:
        - impact_magnitude >= 0.7 (high impact) AND relevance_score >= 0.7 (direct)
          AND source_credibility in {tier1, tier2} → auto_approved
        - Otherwise leave status unchanged.
        - Never auto-approve an already-rejected event.
        """
        if desired_status == "rejected":
            return desired_status
        impact = event.impact_magnitude
        relevance = event.relevance_score
        credibility = (event.source_credibility or "tier2").strip().lower()
        if impact >= 0.70 and relevance >= 0.70 and credibility in {"tier1", "tier2"}:
            return "auto_approved"
        return desired_status

    async def upsert_event(self, event: ExtractedEvent, org_id: str = "default-org") -> IntelligenceEvent:
        """Insert or update an event. Returns the persisted ORM model."""
        db = self._db
        stmt = select(IntelligenceEvent).where(IntelligenceEvent.event_id == event.event_id)
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()

        desired_status = (
            event.review_status or self._default_review_status()
        ).strip().lower()
        final_status = self._compute_auto_approval(event, desired_status)

        payload = {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "headline": event.headline,
            "affected_commodities": event.affected_commodities,
            "direction": event.direction,
            "magnitude_estimate": event.magnitude_estimate,
            "certainty": event.certainty,
            "time_horizon": event.time_horizon,
            "geographic_scope": event.geographic_scope,
            "relevance_to_c5_c9": event.relevance_to_c5_c9,
            "key_entities": event.key_entities,
            "key_information": (event.key_information or "").strip(),
            "causal_chain_hint": event.causal_chain_hint or "",
            "source_url": event.source_url or "",
            "source_credibility": event.source_credibility or "tier2",
            "full_text": event.full_text or "",
            "raw_quote": event.raw_quote or "",
            "detected_at": event.detected_at,
            "impact_magnitude": event.impact_magnitude,
            "relevance_score": event.relevance_score,
            "review_status": final_status,
            "usefulness_score": int(event.usefulness_score or 0),
            "reviewed_by": (event.reviewed_by or "").strip(),
            "reviewed_at": (event.reviewed_at or "").strip(),
            "review_note": (event.review_note or "").strip(),
            "org_id": org_id,
        }

        if final_status == "auto_approved" and desired_status != "auto_approved":
            logger.info(
                "[auto-approval] %s → auto_approved (impact=%.2f, relevance=%.2f, credibility=%s)",
                event.event_id,
                event.impact_magnitude,
                event.relevance_score,
                event.source_credibility,
            )

        if existing:
            for key, val in payload.items():
                if key == "event_id":
                    continue
                if key == "source_url" and not val:
                    continue
                if key in ("review_status", "usefulness_score", "reviewed_by",
                           "reviewed_at", "review_note") and not val:
                    continue
                # Phase 5: Never downgrade auto_approved or rejected events
                if key == "review_status":
                    existing_status = getattr(existing, "review_status", "")
                    if existing_status in ("auto_approved", "rejected") and val not in ("auto_approved", "rejected"):
                        logger.debug(
                            "[auto-approval] preserving existing status=%s for %s (incoming=%s)",
                            existing_status, event.event_id, val,
                        )
                        continue
                setattr(existing, key, val)
            existing.updated_date = datetime.now(timezone.utc)
            await db.flush()
            return existing
        else:
            new_event = IntelligenceEvent(**payload)
            db.add(new_event)
            await db.flush()
            return new_event

    async def list_recent_events(
        self,
        hours: int = 24,
        min_impact: Optional[float] = None,
        review_status: Optional[str] = None,
        min_usefulness_score: Optional[int] = None,
        limit: int = 50,
        org_id: Optional[str] = None,
    ) -> List[ExtractedEvent]:
        """List events detected within the last ``hours`` hours."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        conditions = [IntelligenceEvent.detected_at >= cutoff]
        if min_impact is not None:
            conditions.append(IntelligenceEvent.impact_magnitude >= min_impact)
        if review_status:
            conditions.append(IntelligenceEvent.review_status == review_status.strip().lower())
        if min_usefulness_score is not None:
            conditions.append(IntelligenceEvent.usefulness_score >= int(min_usefulness_score))
        if org_id:
            conditions.append(IntelligenceEvent.org_id == org_id)

        stmt = (
            select(IntelligenceEvent)
            .where(and_(*conditions))
            .order_by(IntelligenceEvent.detected_at.desc())
            .limit(limit)
        )
        result = await self._db.execute(stmt)
        return [self._row_to_event(row) for row in result.scalars().all()]

    async def list_events_as_of(
        self,
        as_of: str,
        lookback_hours: int = 168,
        min_impact: Optional[float] = None,
        review_status: Optional[str] = None,
        min_usefulness_score: Optional[int] = None,
        limit: int = 80,
        org_id: Optional[str] = None,
    ) -> List[ExtractedEvent]:
        """Return events known at a historical as-of time."""
        as_of_dt = datetime.fromisoformat(str(as_of)[:19])
        cutoff = (as_of_dt - timedelta(hours=lookback_hours)).isoformat()
        conditions = [
            IntelligenceEvent.detected_at >= cutoff,
            IntelligenceEvent.detected_at <= as_of_dt.isoformat(),
        ]
        if min_impact is not None:
            conditions.append(IntelligenceEvent.impact_magnitude >= min_impact)
        if review_status:
            conditions.append(IntelligenceEvent.review_status == review_status.strip().lower())
        if min_usefulness_score is not None:
            conditions.append(IntelligenceEvent.usefulness_score >= int(min_usefulness_score))
        if org_id:
            conditions.append(IntelligenceEvent.org_id == org_id)

        stmt = (
            select(IntelligenceEvent)
            .where(and_(*conditions))
            .order_by(IntelligenceEvent.detected_at.desc())
            .limit(limit)
        )
        result = await self._db.execute(stmt)
        return [self._row_to_event(row) for row in result.scalars().all()]

    async def get_event(self, event_id: str) -> Optional[ExtractedEvent]:
        """Get a single event by its event_id."""
        stmt = select(IntelligenceEvent).where(IntelligenceEvent.event_id == event_id)
        result = await self._db.execute(stmt)
        row = result.scalar_one_or_none()
        if not row:
            return None
        return self._row_to_event(row)

    async def review_event(
        self,
        event_id: str,
        *,
        review_status: str,
        usefulness_score: Optional[int] = None,
        reviewed_by: str = "",
        review_note: str = "",
    ) -> bool:
        """Update the review status of an event. Returns True if updated."""
        status = (review_status or "").strip().lower()
        if status not in {"pending", "approved", "rejected"}:
            raise ValueError("review_status must be pending|approved|rejected")

        score = usefulness_score
        if score is not None:
            score = max(0, min(5, int(score)))

        values: dict[str, Any] = {
            "review_status": status,
            "reviewed_by": reviewed_by.strip(),
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "review_note": review_note.strip(),
            "updated_date": datetime.now(timezone.utc),
        }
        if score is not None:
            values["usefulness_score"] = score

        stmt = (
            update(IntelligenceEvent)
            .where(IntelligenceEvent.event_id == event_id)
            .values(**values)
        )
        result = await self._db.execute(stmt)
        await self._db.flush()
        return result.rowcount > 0

    async def remove_event(
        self,
        event_id: str,
        *,
        reviewed_by: str = "",
        review_note: str = "",
    ) -> bool:
        """Reject an event via review_status='rejected'."""
        note = (review_note or "").strip()
        if not note:
            note = "Removed by admin moderation"
        return await self.review_event(
            event_id,
            review_status="rejected",
            usefulness_score=0,
            reviewed_by=reviewed_by,
            review_note=note,
        )

    async def get_review_summary(self, hours: int = 168, org_id: Optional[str] = None) -> dict[str, Any]:
        """Get review status summary for events in the last ``hours`` hours."""
        events = await self.list_recent_events(hours=hours, org_id=org_id, limit=10000)
        buckets: dict[str, list] = {"approved": [], "auto_approved": [], "pending": [], "rejected": []}
        for ev in events:
            status = ev.review_status or "approved"
            buckets.setdefault(status, []).append(ev)
        total = len(events)
        counts = {k: len(v) for k, v in buckets.items()}
        usefulness_scores = [ev.usefulness_score for ev in events if ev.usefulness_score]
        avg_usefulness = (
            round(sum(usefulness_scores) / len(usefulness_scores), 3)
            if usefulness_scores else 0
        )
        return {
            "hours": hours,
            "total_events": total,
            "approved_count": counts.get("approved", 0),
            "auto_approved_count": counts.get("auto_approved", 0),
            "pending_count": counts.get("pending", 0),
            "rejected_count": counts.get("rejected", 0),
            "usefulness_avg": avg_usefulness,
        }

    async def upsert_ingestion_status(
        self, agent_name: str, status: dict[str, Any], org_id: str = "default-org"
    ) -> IntelligenceIngestionStatus:
        """Insert or update ingestion status for a background agent."""
        db = self._db
        stmt = select(IntelligenceIngestionStatus).where(
            and_(
                IntelligenceIngestionStatus.agent_name == agent_name,
                IntelligenceIngestionStatus.org_id == org_id,
            )
        )
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()

        payload = {
            "running": bool(status.get("running")),
            "cycle_in_progress": bool(status.get("cycle_in_progress")),
            "scan_interval_seconds": status.get("scan_interval_seconds"),
            "last_started_at": status.get("last_started_at"),
            "last_completed_at": status.get("last_completed_at"),
            "last_success_at": status.get("last_success_at"),
            "last_error": status.get("last_error"),
            "last_cycle_duration_ms": status.get("last_cycle_duration_ms"),
            "last_articles_seen": int(status.get("last_articles_seen") or 0),
            "last_events_extracted": int(status.get("last_events_extracted") or 0),
            "last_events_stored": int(status.get("last_events_stored") or 0),
            "next_run_at": status.get("next_run_at"),
        }

        if existing:
            for key, val in payload.items():
                setattr(existing, key, val)
            existing.updated_date = datetime.now(timezone.utc)
            await db.flush()
            return existing
        else:
            new_status = IntelligenceIngestionStatus(
                agent_name=agent_name,
                org_id=org_id,
                **payload,
            )
            db.add(new_status)
            await db.flush()
            return new_status

    async def get_ingestion_status(
        self, agent_name: str, org_id: str = "default-org"
    ) -> dict[str, Any]:
        """Get ingestion status for a specific agent."""
        stmt = select(IntelligenceIngestionStatus).where(
            and_(
                IntelligenceIngestionStatus.agent_name == agent_name,
                IntelligenceIngestionStatus.org_id == org_id,
            )
        )
        result = await self._db.execute(stmt)
        row = result.scalar_one_or_none()
        if not row:
            return {}
        return {
            "running": row.running,
            "cycle_in_progress": row.cycle_in_progress,
            "scan_interval_seconds": row.scan_interval_seconds,
            "last_started_at": row.last_started_at,
            "last_completed_at": row.last_completed_at,
            "last_success_at": row.last_success_at,
            "last_error": row.last_error,
            "last_cycle_duration_ms": row.last_cycle_duration_ms,
            "last_articles_seen": row.last_articles_seen,
            "last_events_extracted": row.last_events_extracted,
            "last_events_stored": row.last_events_stored,
            "next_run_at": row.next_run_at,
        }

    async def get_ingestion_freshness(
        self,
        agent_name: str,
        stale_after_hours: int = 2,
        org_id: str = "default-org",
    ) -> dict[str, Any]:
        """Check freshness of ingestion data."""
        status = await self.get_ingestion_status(agent_name, org_id=org_id)
        last_success_raw = str(status.get("last_success_at") or "").strip()
        if not last_success_raw:
            return {
                "state": "unknown",
                "stale": True,
                "age_minutes": None,
                "last_success_at": None,
                "stale_after_hours": stale_after_hours,
            }
        try:
            last_success = datetime.fromisoformat(last_success_raw)
            age = datetime.now(timezone.utc) - last_success
            age_minutes = max(0, int(age.total_seconds() // 60))
            stale = age > timedelta(hours=stale_after_hours)
            return {
                "state": "stale" if stale else "fresh",
                "stale": stale,
                "age_minutes": age_minutes,
                "last_success_at": last_success_raw,
                "stale_after_hours": stale_after_hours,
            }
        except ValueError:
            return {
                "state": "invalid_timestamp",
                "stale": True,
                "age_minutes": None,
                "last_success_at": last_success_raw,
                "stale_after_hours": stale_after_hours,
            }

    @staticmethod
    def _row_to_event(row: IntelligenceEvent) -> ExtractedEvent:
        """Convert an ORM IntelligenceEvent row to an ExtractedEvent dataclass."""
        return ExtractedEvent(
            event_id=row.event_id,
            event_type=row.event_type,
            headline=row.headline,
            affected_commodities=row.affected_commodities or [],
            direction=row.direction or "uncertain",
            magnitude_estimate=row.magnitude_estimate or "medium",
            certainty=row.certainty or "likely",
            time_horizon=row.time_horizon or "short",
            geographic_scope=row.geographic_scope or "global",
            relevance_to_c5_c9=row.relevance_to_c5_c9 or "indirect",
            key_entities=row.key_entities or [],
            key_information=row.key_information or "",
            causal_chain_hint=row.causal_chain_hint or "",
            source_url=row.source_url or "",
            source_credibility=row.source_credibility or "tier2",
            full_text=row.full_text or "",
            raw_quote=row.raw_quote or "",
            detected_at=row.detected_at or datetime.now(timezone.utc).isoformat(),
            review_status=row.review_status or "approved",
            usefulness_score=int(row.usefulness_score or 0),
            reviewed_by=row.reviewed_by or "",
            reviewed_at=row.reviewed_at or "",
            review_note=row.review_note or "",
        )
