"""Regression tests: the dispatcher must self-heal tasks whose status is a
non-canonical value instead of silently skipping them forever.

Root cause (2026-08-17): the LLM persisted ``status="running"`` on task
``15a13b60``; ``_tick()`` filters on ``status == "active"`` so the task was
invisible to the dispatcher on every tick — ``next_run_at`` advanced forever
and "Last run" stayed empty.

The self-heal sweep (before the due query) promotes any scheduled task with a
non-canonical status back to ``"active"`` so it becomes eligible in the same
tick. Valid statuses (e.g. ``"paused"``) and manual-only tasks (no
``next_run_at``) are left untouched.
"""
import os, sys, uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest
from sqlalchemy import delete, insert

from app.database import SessionLocal, engine
from app.models.base import Base
from app.models.automation_task import AutomationTask
import app.services.automation_dispatcher as dispatcher_mod


def _insert_task(db, name: str, status: str | None, next_run_at) -> str:
    """Insert a minimal scheduled AutomationTask row; return its id.

    A status of ``None`` is written through the Core insert so a genuine
    NULL is stored. The ORM path would apply the column default
    (``status: default="paused"``) for a None attribute and silently mask
    the null-status case this test targets.
    """
    if status is None:
        task_id = str(uuid.uuid4())
        db.execute(
            insert(AutomationTask).values(
                id=task_id,
                name=name,
                type="custom",
                description="test task",
                schedule="0 * * * *",
                cron_expression="0 * * * *",
                timezone="UTC",
                next_run_at=next_run_at,
                status=None,  # explicit None -> real NULL (Core, no default)
                output_format="html",
                notify_chat="true",
                org_id="test-org",
                app_id="test-app",
                created_by_id="u-test",
            )
        )
        db.commit()
        return task_id

    task = AutomationTask(
        name=name,
        type="custom",
        description="test task",
        schedule="0 * * * *",
        cron_expression="0 * * * *",
        timezone="UTC",
        next_run_at=next_run_at,
        status=status,
        output_format="html",
        notify_chat="true",
        org_id="test-org",
        app_id="test-app",
        created_by_id="u-test",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task.id


@pytest.fixture(autouse=True)
def _clean_slate():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        db.execute(delete(AutomationTask))
        db.commit()
    finally:
        db.close()
    # Force a deterministic tick sequence for the reaper gate.
    dispatcher_mod._tick_count = 0
    yield


async def _run_tick(inserts: list[tuple[str, str | None, datetime]]):
    """Insert rows, run one tick with mocked side effects, return (db, fire_mock)."""
    db = SessionLocal()
    try:
        ids = [_insert_task(db, *row) for row in inserts]
        with (
            patch.object(dispatcher_mod, "_reap_stale_executions", new=AsyncMock()),
            patch.object(dispatcher_mod, "_fire", new=AsyncMock()) as fire_mock,
        ):
            await dispatcher_mod._tick()
        return db, ids, fire_mock
    finally:
        db.close()


async def _status_of(db, task_id: str) -> str | None:
    row = db.query(AutomationTask).filter(AutomationTask.id == task_id).first()
    assert row is not None, "task row missing after tick"
    return row.status


def test_tick_promotes_running_status_and_fires():
    """The exact production failure: status='running' -> 'active' + fired."""
    import asyncio

    async def _case():
        db, ids, fire_mock = await _run_tick([
            ("Running task", "running", datetime(2020, 1, 1)),
        ])
        assert await _status_of(db, ids[0]) == "active"
        assert fire_mock.await_count == 1
        fired_task = fire_mock.await_args.args[1]
        assert fired_task.id == ids[0]

    asyncio.run(_case())


def test_tick_promotes_typo_status_and_fires():
    import asyncio

    async def _case():
        db, ids, fire_mock = await _run_tick([
            ("Typo task", "actve", datetime(2020, 1, 1)),
        ])
        assert await _status_of(db, ids[0]) == "active"
        assert fire_mock.await_count == 1

    asyncio.run(_case())


def test_tick_promotes_empty_status_and_fires():
    import asyncio

    async def _case():
        db, ids, fire_mock = await _run_tick([
            ("Empty status task", "", datetime(2020, 1, 1)),
        ])
        assert await _status_of(db, ids[0]) == "active"
        assert fire_mock.await_count == 1

    asyncio.run(_case())


def test_tick_promotes_null_status_and_fires():
    import asyncio

    async def _case():
        db, ids, fire_mock = await _run_tick([
            ("Null status task", None, datetime(2020, 1, 1)),
        ])
        assert await _status_of(db, ids[0]) == "active"
        assert fire_mock.await_count == 1

    asyncio.run(_case())


def test_tick_leaves_valid_paused_untouched():
    """A canonical 'paused' task must NOT be auto-promoted or fired."""
    import asyncio

    async def _case():
        db, ids, fire_mock = await _run_tick([
            ("Paused task", "paused", datetime(2020, 1, 1)),
        ])
        assert await _status_of(db, ids[0]) == "paused"
        assert fire_mock.await_count == 0

    asyncio.run(_case())


def test_tick_leaves_manual_task_with_invalid_status_untouched():
    """Manual-only tasks (next_run_at NULL) are out of scope for self-heal."""
    import asyncio

    async def _case():
        db, ids, fire_mock = await _run_tick([
            ("Manual running task", "running", None),
        ])
        assert await _status_of(db, ids[0]) == "running"
        assert fire_mock.await_count == 0

    asyncio.run(_case())


def test_tick_heals_only_invalid_rows_among_valid_ones():
    """A mixed batch: only the non-canonical row is promoted; the active row
    fires; the paused row is untouched."""
    import asyncio

    async def _case():
        db, ids, fire_mock = await _run_tick([
            ("Running task", "running", datetime(2020, 1, 1)),
            ("Active task", "active", datetime(2020, 1, 1)),
            ("Paused task", "paused", datetime(2020, 1, 1)),
        ])
        assert await _status_of(db, ids[0]) == "active"
        assert await _status_of(db, ids[1]) == "active"
        assert await _status_of(db, ids[2]) == "paused"
        # running was healed -> fires; active was already due -> fires.
        assert fire_mock.await_count == 2
        fired_ids = {call.args[1].id for call in fire_mock.await_args_list}
        assert fired_ids == {ids[0], ids[1]}

    asyncio.run(_case())
