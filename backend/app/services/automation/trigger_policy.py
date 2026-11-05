"""Trigger policy — pure, deterministic trigger semantics for the dispatcher (R4).

Normative sources: TS §2 (trigger schema), §6.1a (misfire collapse —
deterministic and bounded), ADR-013 (representative = latest missed slot;
grace measured against it). Kept free of DB and I/O so the decision table
is unit-testable and reusable by the R5 occurrence-based scheduler.

Pre-occurrence note (R4): the collapse is applied at the task level — a
missed slot beyond grace is skipped and the schedule advances to the next
future slot. R5 moves the same policy onto ``automation_occurrences`` rows
without changing the decision function.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

DEFAULT_GRACE_SECONDS = 3600


@dataclass(frozen=True)
class MisfireDecision:
    action: str          # "fire_late" | "skip"
    catchup: bool        # skip + recurring ⇒ advance to the next future slot;
                         # skip + once ⇒ terminal (no next slot exists)


def resolve_trigger(task) -> str:
    """Resolve the effective trigger type, with legacy-row compatibility.

    Legacy rows (``trigger_type IS NULL``): a cron/schedule string means
    ``schedule``; anything else is ``manual``. Explicit values win.
    """
    explicit = getattr(task, "trigger_type", None)
    if explicit in ("schedule", "once", "manual"):
        return explicit
    if (getattr(task, "cron_expression", None) or getattr(task, "schedule", None)):
        return "schedule"
    return "manual"


def grace_seconds(task) -> int:
    """Grace window: spec-defined when valid, else the platform default (1h)."""
    spec = getattr(task, "spec", None) or {}
    trigger = spec.get("trigger") or {}
    value = trigger.get("grace_window_seconds")
    if isinstance(value, int) and 0 <= value <= 7 * 24 * 3600:
        return value
    return DEFAULT_GRACE_SECONDS


def misfire_decision(
    *, due_at: datetime, now: datetime, grace_seconds: int, recurring: bool
) -> MisfireDecision:
    """Deterministic misfire policy for one due slot.

    - Not late (or late within grace, inclusive) → ``fire_late`` (the normal
      fire path; "late" includes on-time firing).
    - Late beyond grace:
      - recurring → ``skip`` with ``catchup=True``: the missed slot is
        collapsed (bounded — never one execution per missed slot); the
        caller advances the schedule to the next future slot.
      - once → ``skip`` with ``catchup=False``: no next slot exists; the
        caller terminalizes without firing.
    """
    late_by = (now - due_at).total_seconds()
    if late_by <= grace_seconds:
        return MisfireDecision(action="fire_late", catchup=False)
    return MisfireDecision(action="skip", catchup=recurring)


@dataclass(frozen=True)
class CollapseDecision:
    """§6.1a full-episode collapse (F-16): the deterministic algorithm over
    the whole missed interval — NOT one decision per slot.

    - ``representative`` = the LATEST missed slot (ADR-013); grace is
      measured against it.
    - ``collapsed_count`` = |S| (bounded backlog: ONE occurrence row per
      definition per episode, by construction).
    - ``advance_to`` = the next future slot (step 6 of the algorithm).
    """
    action: str                     # "fire_late" | "skip"
    representative: Optional[datetime]   # R = max(S); None when S is empty
    collapsed_count: int
    advance_to: Optional[datetime]
    catchup: bool                   # skip + recurring ⇒ advance (caller CASes)


def _enumerate_slots(cron: str, tz_name: str, start: datetime, end: datetime) -> list:
    """Slots of cron/tz in [start, end) — earliest→latest, UTC-naive.

    The existing parser is the single source of truth for slot resolution
    (zoneinfo fold=0, nonexistent local times handled per its pinned
    behavior — §10.33 pins the exact outcomes).
    """
    from app.services.schedule_parser import next_run_at

    slots: list = []
    cursor = start
    for _ in range(10000):  # hard bound: a broken cron can never hang the tick
        slot = next_run_at(cron, after=cursor - timedelta(seconds=1), tz_name=tz_name)
        if slot is None or slot >= end:
            break
        if slot > cursor or not slots:  # strictly forward
            slots.append(slot)
        cursor = slot + timedelta(seconds=1)
        if cursor >= end:
            break
    return slots


def collapse_missed_interval(
    *,
    cron: Optional[str],
    tz: str,
    next_run_at: datetime,
    now: datetime,
    grace_seconds: int,
    policy: str = "fire_once_late",
    recurring: bool = True,
) -> CollapseDecision:
    """§6.1a steps 1–5 (pure): decide what ONE occurrence row represents
    for a whole missed interval.

    Caller responsibilities (steps 6–7): CAS-advance ``next_run_at`` to
    ``advance_to`` in the same transaction as the occurrence write.
    Returns ``None`` when there is nothing to collapse (marker is None or
    not yet due) — the caller proceeds down the normal fire path.
    """
    if next_run_at is None or next_run_at >= now:
        return None
    if cron is None or not recurring:
        # Once-trigger (or unparseable recurring): the single due instant is
        # the whole episode. Past grace ⇒ skipped(misfire), never re-arms.
        late_by = (now - next_run_at).total_seconds()
        if late_by <= grace_seconds and policy == "fire_once_late":
            return CollapseDecision("fire_late", next_run_at, 1, None, False)
        return CollapseDecision("skip", next_run_at, 1, None, False)

    slots = _enumerate_slots(cron, tz, next_run_at, now)
    if not slots:
        # No missed slot inside the interval (e.g. due exactly at now):
        # treat the persisted marker itself as the representative.
        slots = [next_run_at] if next_run_at < now else []
        if not slots:
            return CollapseDecision("skip", None, 0, None, True)

    representative = max(slots)
    staleness = (now - representative).total_seconds()
    advance_to = _enumerate_slots(cron, tz, representative + timedelta(seconds=1),
                                  now + timedelta(days=400))
    nxt = advance_to[0] if advance_to else None

    if policy == "fire_once_late" and staleness <= grace_seconds:
        return CollapseDecision("fire_late", representative, len(slots), nxt, False)
    return CollapseDecision("skip", representative, len(slots), nxt, True)
