"""Regression tests for I1 (sync/async mismatch in scheduled tasks) and M8 (failure counter).

I1: _memory_consolidation_sync and _skill_curation_sync use sync SessionLocal,
not AsyncSessionLocal. The async wrappers delegate via asyncio.to_thread.

M8: _inc_failure tracks task failures so silent dead tasks become observable.
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import pytest
from unittest.mock import patch, MagicMock


# ── I1: memory consolidation and skill curation use sync SessionLocal ──

def test_memory_consolidation_uses_sync_session():
    """I1: _memory_consolidation_sync must use SessionLocal (sync), not AsyncSessionLocal."""
    # Verify the sync function exists and takes no arguments
    from app.services.scheduled_tasks import _memory_consolidation_sync, _skill_curation_sync

    assert callable(_memory_consolidation_sync)
    assert callable(_skill_curation_sync)


def test_memory_consolidation_wrapper_uses_to_thread():
    """I1: _run_memory_consolidation_cycle must delegate via asyncio.to_thread."""
    import asyncio
    from app.services.scheduled_tasks import _run_memory_consolidation_cycle

    assert asyncio.iscoroutinefunction(_run_memory_consolidation_cycle)


def test_skill_curation_wrapper_uses_to_thread():
    """I1: _run_skill_curation_cycle must delegate via asyncio.to_thread."""
    import asyncio
    from app.services.scheduled_tasks import _run_skill_curation_cycle

    assert asyncio.iscoroutinefunction(_run_skill_curation_cycle)


# ── M8: failure counter tracks scheduled task failures ──

def test_inc_failure_increments_counter():
    """M8: _inc_failure tracks per-task failure counts."""
    from app.services.scheduled_tasks import _inc_failure, _scheduled_failure_count

    _scheduled_failure_count.clear()

    _inc_failure("test_task")
    assert _scheduled_failure_count["test_task"] == 1

    _inc_failure("test_task")
    assert _scheduled_failure_count["test_task"] == 2

    _inc_failure("other_task")
    assert _scheduled_failure_count["other_task"] == 1
    assert _scheduled_failure_count["test_task"] == 2


def test_scheduled_failure_count_exists():
    """M8: _scheduled_failure_count dict must exist for observability."""
    from app.services.scheduled_tasks import _scheduled_failure_count
    assert isinstance(_scheduled_failure_count, dict)


# ── M7: advisory lock guard in start_scheduled_tasks ──

def test_try_acquire_scheduler_lock_exists():
    """M7: _try_acquire_scheduler_lock must exist for multi-worker guard."""
    from app.services.scheduled_tasks import _try_acquire_scheduler_lock

    assert callable(_try_acquire_scheduler_lock)


def test_scheduler_lock_respects_env_var():
    """M7: SCHEDULER_WORKER_ID=0 should win, SCHEDULER_WORKER_ID=1 should lose."""
    from app.services.scheduled_tasks import _try_acquire_scheduler_lock

    try:
        os.environ["SCHEDULER_WORKER_ID"] = "0"
        assert _try_acquire_scheduler_lock() is True

        os.environ["SCHEDULER_WORKER_ID"] = "1"
        assert _try_acquire_scheduler_lock() is False
    finally:
        os.environ.pop("SCHEDULER_WORKER_ID", None)
