"""Ontology sensing pipeline (P3-2) — IBM's "continuous sense→decide→act".

Sense:  a sensor runs a read-only watch SQL against a tenant database
        (any connector) on a schedule — via QueryService, so the same
        read-only enforcement as the chat agent applies.
Decide: compare the numeric result against a threshold. Only a breach
        STATE CHANGE fires (spam-free: breach notifies once, recovery
        notifies once).
Act:    notify_object_change fans out through ontology links to the
        agents bound to the watched object's linked domains — audit-
        logged; those agents then propose mitigations within their
        guardrails (writes still require approval).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_OPS = {
    "lt": lambda v, t: v < t,
    "lte": lambda v, t: v <= t,
    "gt": lambda v, t: v > t,
    "gte": lambda v, t: v >= t,
    "eq": lambda v, t: v == t,
}


def _coerce_value(raw) -> float | None:
    """Coerce the first cell of the watch result to a float. Booleans
    count (0/1); None is honest failure, not zero."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _load_state(sensor) -> dict:
    try:
        state = json.loads(sensor.last_state_json or "{}")
        return state if isinstance(state, dict) else {}
    except (TypeError, ValueError):
        return {}


def evaluate_sensor(db: Session, sensor) -> dict:
    """Run one sensor: execute watch SQL, evaluate threshold, detect
    state transition, record a run, and fan out on change. Never raises
    — a broken sensor must not kill the scheduler loop."""
    from app.models.ontology_sensor import OntologySensorRun
    from app.services.db.query_service import QueryService

    now = datetime.now(timezone.utc).isoformat()
    value: float | None = None
    breached = False
    transition = "none"
    summary: str | None = None
    error: str | None = None

    try:
        result = QueryService(db).execute(
            sensor.kb_id, sensor.watch_sql, max_rows=1, timeout_s=20)
        rows = result.get("rows") or []
        if not rows:
            raise ValueError("watch SQL returned no rows")
        first = rows[0]
        raw = list(first.values())[0] if isinstance(first, dict) else first[0]
        value = _coerce_value(raw)
        if value is None:
            raise ValueError(f"watch SQL result is not numeric: {raw!r}")
        op_fn = _OPS.get((sensor.op or "lt").lower())
        if not op_fn:
            raise ValueError(f"unknown operator: {sensor.op}")
        breached = op_fn(value, float(sensor.threshold))
    except Exception as exc:  # noqa: BLE001 — sensor errors are recorded, not raised
        error = str(exc)[:500]
        logger.warning("sensor %s (%s) failed: %s", sensor.id, sensor.name, error)

    if error is None:
        prev = _load_state(sensor)
        prev_breached = bool(prev.get("breached"))
        if breached and not prev_breached:
            transition = "breach"
        elif not breached and prev_breached:
            transition = "recovery"

        if transition in ("breach", "recovery"):
            verb = ("BREACH" if transition == "breach" else "RECOVERED")
            summary = (
                f"[{verb}] sensor {sensor.name!r}: watched value "
                f"{value} {sensor.op} threshold {sensor.threshold} "
                f"({verb.lower() if transition == 'breach' else 'back within limits'})"
            )
            try:
                from app.services.ontology.fanout import notify_object_change

                out = notify_object_change(
                    db, sensor.org_id, sensor.object_name, summary,
                    changed_by=f"sensor:{sensor.id}", team_id=sensor.team_id,
                )
                summary = f"{summary} — notified: {out.get('notified', [])}"
            except Exception as exc:  # noqa: BLE001 — fan-out failure ≠ run failure
                summary += f" — fan-out failed: {exc}"
                logger.warning("sensor %s fan-out failed: %s", sensor.id, exc)

    try:
        run = OntologySensorRun(
            org_id=sensor.org_id, sensor_id=sensor.id, value=value,
            breached=breached, transition=transition,
            change_summary=summary, error=error,
        )
        db.add(run)
        if error is None:
            sensor.last_state_json = json.dumps(
                {"breached": breached, "value": value})  # full reassign: JSON
        sensor.last_run_at = now
        db.commit()
    except Exception as exc:  # noqa: BLE001 — bookkeeping must not raise
        db.rollback()
        logger.warning("sensor %s run bookkeeping failed: %s", sensor.id, exc)

    return {
        "sensor_id": sensor.id, "sensor_name": sensor.name,
        "value": value, "breached": breached, "transition": transition,
        "change_summary": summary, "error": error,
    }


def run_due_sensors(db: Session) -> list[dict]:
    """Run every enabled sensor whose schedule is due. Called by the
    60s background loop; each sensor's own schedule_seconds gates it."""
    from app.models.ontology_sensor import OntologySensor

    sensors = (
        db.query(OntologySensor)
        .filter(
            OntologySensor.org_id.isnot(None),
            OntologySensor.is_deleted == False,  # noqa: E712
            OntologySensor.enabled == True,  # noqa: E712
        )
        .all()
    )
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for sensor in sensors:
        if sensor.last_run_at:
            try:
                last = datetime.fromisoformat(sensor.last_run_at)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if (now - last).total_seconds() < max(
                        60, int(sensor.schedule_seconds or 3600)):
                    continue
            except (TypeError, ValueError):
                pass  # unparseable timestamp → run it
        out.append(evaluate_sensor(db, sensor))
    return out


# ── background loop (mirrors catalog_watchdog's pattern) ───────────────────

POLL_INTERVAL_SECONDS = 60


async def run_ontology_sensors(stop_event: asyncio.Event) -> None:
    from app.database import SessionLocal

    logger.info("ontology sensing loop started (poll every %ss)",
                POLL_INTERVAL_SECONDS)
    while True:
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
            break  # shutdown signalled
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            break

        def _tick():
            db = SessionLocal()
            try:
                return run_due_sensors(db)
            finally:
                db.close()

        try:
            results = await asyncio.to_thread(_tick)
            fired = [r for r in results if r["transition"] != "none"]
            if fired:
                logger.info("sensing tick: %d ran, %d fired fan-out",
                            len(results), len(fired))
        except Exception as exc:  # noqa: BLE001 — the loop never dies
            logger.warning("sensing tick failed (non-fatal): %s", exc)
    logger.info("ontology sensing loop stopped")
