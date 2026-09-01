"""Tests for scheduled tasks.

The alerting half of this file was removed with the alerts system (2026-08-27).
"""
import asyncio
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.scheduled_tasks import (
    MEMORY_CONSOLIDATION_INTERVAL,
    SKILL_CURATION_INTERVAL,
    start_scheduled_tasks,
    stop_scheduled_tasks,
)


# -- Scheduled task tests --

def test_schedule_intervals():
    """Schedule intervals are reasonable values."""
    assert MEMORY_CONSOLIDATION_INTERVAL >= 60  # at least 1 minute
    assert SKILL_CURATION_INTERVAL > MEMORY_CONSOLIDATION_INTERVAL


def test_start_scheduled_tasks_no_loop():
    """start_scheduled_tasks returns without error when no event loop."""
    # Outside an event loop
    start_scheduled_tasks()
    # Should not raise — just logs a warning


def test_start_stop_scheduled_tasks_with_loop():
    """start and stop scheduled tasks within an event loop."""
    async def _test():
        start_scheduled_tasks()
        # Give tasks a tiny moment to register
        await asyncio.sleep(0.01)
        await stop_scheduled_tasks()

    asyncio.run(_test())
