"""Regression tests: create_automation must never persist a non-canonical
AutomationTask.status.

Root cause (2026-08-17): the LLM passed ``status="running"`` to
``_create_automation`` and it was stored verbatim. The dispatcher filters on
``status == "active"`` so the task was silently skipped on every tick forever.

This file pins the create-time coercion:
  - invalid values (e.g. "running") are coerced to the canonical default
  - valid values pass through untouched
  - a missing status uses the default ("active" with a cron, else "paused")
"""
import os, sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest
from sqlalchemy import delete

from app.database import SessionLocal, engine
from app.models.base import Base
from app.models.automation_task import AutomationTask
from app.services.agent_tools import TOOL_CONTEXT, _create_automation


@pytest.fixture(autouse=True)
def _clean_slate():
    """Fresh schema + empty automation_tasks for every test."""
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        db.execute(delete(AutomationTask))
        db.commit()
    finally:
        db.close()
    # Reset the module-level tool context so sibling tests don't leak state.
    TOOL_CONTEXT.clear()
    yield
    TOOL_CONTEXT.clear()


def _create(name: str, schedule: str, status: str | None):
    """Thin wrapper around _create_automation for the status-coercion tests."""
    args: dict = {"name": name, "type": "custom", "schedule": schedule}
    if status is not None:
        args["status"] = status
    db = SessionLocal()
    try:
        result = _create_automation(args, db=db, user_id="u-test")
    finally:
        db.close()
    return result


def test_invalid_status_running_coerced_to_active_with_cron():
    """The exact production failure: status='running' + cron -> 'active'."""
    result = _create("Invalid running", "0 * * * *", "running")
    assert result["status"] == "active"


def test_invalid_status_coerced_to_paused_without_cron():
    """Invalid status + no cron -> the non-cron default 'paused'."""
    result = _create("Invalid manual", "manual", "running")
    assert result["status"] == "paused"


def test_valid_status_passes_through():
    """A canonical value must not be rewritten."""
    result = _create("Valid paused", "0 * * * *", "paused")
    assert result["status"] == "paused"


def test_missing_status_defaults_to_active_with_cron():
    result = _create("Missing status cron", "0 * * * *", None)
    assert result["status"] == "active"


def test_missing_status_defaults_to_paused_without_cron():
    result = _create("Missing status manual", "manual", None)
    assert result["status"] == "paused"


def test_typo_status_coerced_to_active():
    """A near-miss typo ('actve') is also a non-canonical value."""
    result = _create("Typo status", "0 * * * *", "actve")
    assert result["status"] == "active"


def test_canonical_statuses_are_lowercase_source_of_truth():
    """Guard the single source of truth used by the DB CHECK constraint."""
    assert AutomationTask.VALID_STATUSES == (
        "active", "paused", "failed", "completed",
    )
