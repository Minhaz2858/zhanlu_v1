"""R4 — Trigger fidelity: once/manual triggers, misfire collapse, degradation.

Pure-policy tests (trigger_policy) + dispatcher integration (once-fire
terminal semantics, degraded skip) + spec→definition trigger mapping.
DST correctness is pinned against the existing schedule_parser.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import Base  # noqa: E402
from app.models.automation_task import AutomationTask  # noqa: E402
from app.services.automation.trigger_policy import (  # noqa: E402
    grace_seconds,
    misfire_decision,
    resolve_trigger,
)


def _task(**kw) -> AutomationTask:
    base = dict(name=f"R4 {uuid.uuid4().hex[:6]}", type="report", prompt="p",
                status="active")
    base.update(kw)
    return AutomationTask(**base)


# ---------------------------------------------------------------------------
# resolve_trigger — legacy compatibility + typed triggers
# ---------------------------------------------------------------------------


def test_resolve_legacy_task_with_cron_is_schedule():
    t = _task(cron_expression="0 8 * * *")
    assert resolve_trigger(t) == "schedule"


def test_resolve_legacy_task_without_cron_is_manual():
    t = _task(cron_expression=None, schedule=None)
    assert resolve_trigger(t) == "manual"


def test_resolve_explicit_once_and_manual():
    assert resolve_trigger(_task(trigger_type="once", run_at=datetime(2027, 1, 1))) == "once"
    assert resolve_trigger(_task(trigger_type="manual")) == "manual"
    assert resolve_trigger(_task(trigger_type="schedule", cron_expression="0 8 * * *")) == "schedule"


# ---------------------------------------------------------------------------
# misfire_decision (§6.1a semantics at task level, pre-occurrence)
# ---------------------------------------------------------------------------


NOW = datetime(2026, 11, 3, 9, 0, 0, tzinfo=timezone.utc)


def test_within_grace_fires_late():
    decision = misfire_decision(
        due_at=NOW - timedelta(minutes=10), now=NOW, grace_seconds=3600, recurring=True
    )
    assert decision.action == "fire_late"


def test_exactly_at_grace_boundary_fires():
    decision = misfire_decision(
        due_at=NOW - timedelta(seconds=3600), now=NOW, grace_seconds=3600, recurring=True
    )
    assert decision.action == "fire_late"


def test_beyond_grace_recurring_skips_and_advances():
    decision = misfire_decision(
        due_at=NOW - timedelta(hours=3), now=NOW, grace_seconds=3600, recurring=True
    )
    assert decision.action == "skip"
    assert decision.catchup is True  # caller recomputes the next future slot


def test_beyond_grace_once_skips_without_catchup():
    decision = misfire_decision(
        due_at=NOW - timedelta(hours=3), now=NOW, grace_seconds=3600, recurring=False
    )
    assert decision.action == "skip"
    assert decision.catchup is False  # once: no next slot; terminal


def test_future_due_is_not_a_misfire():
    decision = misfire_decision(
        due_at=NOW + timedelta(minutes=5), now=NOW, grace_seconds=3600, recurring=True
    )
    assert decision.action == "fire_late"  # not late at all — normal fire path


# ---------------------------------------------------------------------------
# grace_seconds — spec-driven with a legacy default
# ---------------------------------------------------------------------------


def test_grace_from_spec():
    t = _task(spec={"trigger": {"grace_window_seconds": 600}})
    assert grace_seconds(t) == 600


def test_grace_legacy_default():
    assert grace_seconds(_task()) == 3600
    assert grace_seconds(_task(spec={"trigger": {"grace_window_seconds": -5}})) == 3600


# ---------------------------------------------------------------------------
# Dispatcher integration: once-trigger terminal fire + degraded skip
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture
def no_spawn(monkeypatch):
    """Neuter the executor spawn — we only verify claim/side effects."""
    import app.services.automation_dispatcher as disp

    async def _noop(execution_id):
        return None

    monkeypatch.setattr(disp, "_run_executor", _noop)


@pytest.mark.asyncio
async def test_once_trigger_fires_then_completes(db, no_spawn):
    import app.services.automation_dispatcher as disp

    # Within the grace window → normal fire path, then terminal.
    due = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=30)
    t = _task(trigger_type="once", run_at=due, next_run_at=due, status="active")
    db.add(t)
    db.commit()
    await disp._fire(db, t, datetime.now(timezone.utc))
    db.expire_all()
    t = db.query(AutomationTask).get(t.id)
    assert t.status == "completed"
    assert t.next_run_at is None
    from app.models.automation_execution import AutomationExecution

    assert db.query(AutomationExecution).filter_by(automation_task_id=t.id).count() == 1


@pytest.mark.asyncio
async def test_recurring_trigger_keeps_scheduling(db, no_spawn):
    import app.services.automation_dispatcher as disp

    # next_run_at is stored UTC-naive — build the due instant naive.
    t = _task(trigger_type="schedule", cron_expression="0 8 * * *",
              timezone="UTC",
              next_run_at=datetime.utcnow() - timedelta(minutes=1))
    db.add(t)
    db.commit()
    await disp._fire(db, t, datetime.now(timezone.utc))
    db.expire_all()
    t = db.query(AutomationTask).get(t.id)
    assert t.status == "active"
    assert t.next_run_at is not None and t.next_run_at > datetime.utcnow()


@pytest.mark.asyncio
async def test_misfire_beyond_grace_skips_slot_advances(db, no_spawn, monkeypatch):
    """§6.1a fixed-clock skip case: daily 08:00 UTC cron observed 4h late —
    staleness (4h) > grace (1h) ⇒ one skipped occurrence, schedule advanced."""
    import app.services.automation_dispatcher as disp
    from datetime import datetime as dt

    monkeypatch.setattr(
        "app.services.automation_dispatcher.datetime",
        type("FrozenDT", (), {
            "now": staticmethod(lambda tz=None: dt(2026, 11, 4, 12, 0, tzinfo=timezone.utc)),
        }),
    )
    t = _task(trigger_type="schedule", cron_expression="0 8 * * *",
              timezone="UTC", next_run_at=dt(2026, 11, 4, 8, 0),
              spec={"trigger": {"grace_window_seconds": 3600}})
    db.add(t)
    db.commit()
    await disp._fire(db, t, dt(2026, 11, 4, 12, 0, tzinfo=timezone.utc))
    db.expire_all()
    t = db.query(AutomationTask).get(t.id)
    from app.models.automation_execution import AutomationExecution
    from app.models.automation_scheduling import AutomationOccurrence

    # Episode collapsed: no execution, ONE skipped occurrence row, next
    # slot advanced to tomorrow 08:00 UTC.
    assert db.query(AutomationExecution).filter_by(automation_task_id=t.id).count() == 0
    occ = db.query(AutomationOccurrence).filter_by(automation_task_id=t.id).first()
    assert occ is not None and occ.status == "skipped"
    assert occ.cancel_reason == "misfire"
    assert occ.misfire_collapsed_count == 1
    assert t.next_run_at == dt(2026, 11, 5, 8, 0)


@pytest.mark.asyncio
async def test_misfire_fresh_due_fires_once_for_whole_episode(db, no_spawn, monkeypatch):
    """§6.1a example 4: long downtime but the latest missed slot is fresh —
    fires ONCE for the whole episode with the collapsed count recorded."""
    import app.services.automation_dispatcher as disp
    from datetime import datetime as dt

    monkeypatch.setattr(
        "app.services.automation_dispatcher.datetime",
        type("FrozenDT", (), {
            "now": staticmethod(lambda tz=None: dt(2026, 11, 4, 8, 30, tzinfo=timezone.utc)),
        }),
    )
    t = _task(trigger_type="schedule", cron_expression="0 8 * * *",
              timezone="UTC", next_run_at=dt(2026, 11, 1, 8, 0),
              spec={"trigger": {"grace_window_seconds": 3600}})
    db.add(t)
    db.commit()
    await disp._fire(db, t, dt(2026, 11, 4, 8, 30, tzinfo=timezone.utc))
    db.expire_all()
    from app.models.automation_execution import AutomationExecution
    from app.models.automation_scheduling import AutomationOccurrence

    # Exactly one execution (the episode), keyed on the representative slot.
    execs = db.query(AutomationExecution).filter_by(automation_task_id=t.id).all()
    assert len(execs) == 1
    occ = db.query(AutomationOccurrence).filter_by(automation_task_id=t.id).first()
    assert occ is not None and occ.misfire_collapsed_count == 4
    assert "2026-11-04T08:00:00" in occ.occurrence_key
    t = db.query(AutomationTask).get(t.id)
    assert t.next_run_at == dt(2026, 11, 5, 8, 0)


def test_degraded_tasks_excluded_from_due_query(db):
    import app.services.automation_dispatcher as disp

    now = datetime.now(timezone.utc)
    bad = _task(cron_expression="0 8 * * *", next_run_at=now - timedelta(minutes=1),
                schedule_degraded=True, schedule_error="unparseable")
    good = _task(cron_expression="0 8 * * *", next_run_at=now - timedelta(minutes=1))
    db.add_all([bad, good])
    db.commit()
    due = disp._due_tasks(db, now)
    ids = {t.id for t in due}
    assert good.id in ids
    assert bad.id not in ids


# ---------------------------------------------------------------------------
# Spec → definition trigger mapping (control plane, R2 TODO closed)
# ---------------------------------------------------------------------------


def test_confirm_maps_once_trigger(db):
    from app.models.automation_draft import AutomationDraft
    from app.models.project import Project
    from app.services.automation.control_plane import confirm_draft, create_draft
    from types import SimpleNamespace

    db.add(Project(id="prj-1", name="R4 Project"))
    db.commit()
    user = SimpleNamespace(id="u-1", org_id="default-org", role="user")
    spec = {
        "name": "Once Run",
        "project_id": "prj-1",
        "objective": "obj",
        "trigger": {"type": "once", "run_at": "2027-03-01T10:00:00+00:00",
                    "timezone": "Asia/Shanghai"},
        "output": {"format": "html"},
        "delivery": {"chat": True, "email": False},
        "notifications": {"notify_on": "on_failure", "emails": []},
        "approvals": {"policy": "block"},
        "retry": {"max_attempts": 1},
        "budgets": {"wall_clock_seconds": 600, "max_tool_calls": 60, "max_llm_calls": 40},
    }
    draft = create_draft(db, user, spec)
    task, resp = confirm_draft(db, user, draft.id, str(uuid.uuid4()), if_match=1)
    assert task.trigger_type == "once"
    assert task.run_at is not None
    assert task.cron_expression is None
    assert task.next_run_at == task.run_at  # once: dispatcher fires at run_at


def test_confirm_maps_manual_trigger(db):
    from app.models.project import Project
    from app.services.automation.control_plane import confirm_draft, create_draft
    from types import SimpleNamespace

    db.add(Project(id="prj-1", name="R4 Project"))
    db.commit()
    user = SimpleNamespace(id="u-1", org_id="default-org", role="user")
    spec = {
        "name": "Manual Task",
        "project_id": "prj-1",
        "objective": "obj",
        "trigger": {"type": "manual"},
        "output": {"format": "html"},
        "delivery": {"chat": True, "email": False},
        "notifications": {"notify_on": "on_failure", "emails": []},
        "approvals": {"policy": "block"},
        "retry": {"max_attempts": 1},
        "budgets": {"wall_clock_seconds": 600, "max_tool_calls": 60, "max_llm_calls": 40},
    }
    draft = create_draft(db, user, spec)
    task, resp = confirm_draft(db, user, draft.id, str(uuid.uuid4()), if_match=1)
    assert task.trigger_type == "manual"
    assert task.cron_expression is None
    assert task.next_run_at is None  # manual: never auto-fires


# ---------------------------------------------------------------------------
# DST correctness (pin the existing parser's IANA behavior)
# ---------------------------------------------------------------------------


def test_next_run_across_eu_dst_boundary():
    from app.services.schedule_parser import next_run_at

    # EU DST spring-forward: 2026-03-29 02:00 local does not exist in
    # Europe/Berlin. An hourly cron must still resolve every day.
    # next_run_at takes/returns UTC-naive datetimes.
    after = datetime(2026, 3, 28, 23, 0)
    for _ in range(30):
        nxt = next_run_at("0 2 * * *", after=after, tz_name="Europe/Berlin")
        assert nxt > after
        after = nxt


def test_next_run_us_fall_back_day():
    from app.services.schedule_parser import next_run_at

    # US fall-back 2026-11-01: 01:00 local occurs twice — parser must
    # resolve a deterministic next slot (fold=0 semantics per ADR-013).
    after = datetime(2026, 10, 31, 22, 0)
    for _ in range(30):
        nxt = next_run_at("30 1 * * *", after=after, tz_name="America/New_York")
        assert nxt > after
        after = nxt


# ---------------------------------------------------------------------------
# Legacy schedule compatibility (no spec, NL schedule, existing columns)
# ---------------------------------------------------------------------------


def test_legacy_nl_schedule_task_is_schedule_trigger():
    t = _task(schedule="every monday at 9am", cron_expression=None)
    assert resolve_trigger(t) == "schedule"
