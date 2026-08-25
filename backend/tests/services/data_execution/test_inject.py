"""Tests for the v3 dispatcher auto-cache hook (agents.py) + the
``_execution_id`` injection contract.

Service-level only (repo convention — see test_cache.py): we call
``cache_data_execution`` directly and verify the exact merge the hook
performs (``result = {**result, "_execution_id": exec_id}``) carries the
key downstream. No agents.py integration mocks.

The shared DB persists across tests, so each test uses a unique session_id
to avoid counter bleed.
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_inject.db")

import pytest

from app.database import engine
from app.models.data_execution import DataExecution
from app.models.session_state import SessionState
from app.services.data_execution.cache import cache_data_execution
from app.services.data_execution.session_state import SessionStateService


def _ensure_table():
    """Create just the data_executions + session_states tables (idempotent)."""
    DataExecution.__table__.create(bind=engine, checkfirst=True)
    SessionState.__table__.create(bind=engine, checkfirst=True)


@pytest.mark.asyncio
async def test_cache_returns_id_and_merge_carries_execution_id():
    """The hook's exact merge must carry ``_execution_id`` downstream.

    This mirrors what agents.py does after a successful cache write:
    ``result = {**result, "_execution_id": exec_id}`` — the re-assigned
    result keeps every original key and adds the execution id so the
    frontend record + message append can see it.
    """
    _ensure_table()
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        result_in = {
            "query_purpose": "sales trend",
            "rows": [{"month": "2026-01", "value": 42}],
            "sql": "SELECT month, value FROM sales",
            "success": True,
        }
        exec_id = await cache_data_execution(
            db=db,
            session_id="inject-sess-1",
            tool_name="ask_data_agent",
            args={"query": "sales trend"},
            result=result_in,
            summary_text=None,
            org_id="org1",
            app_id="default-app",
        )
        assert exec_id is not None
        assert exec_id.startswith("evt_")

        merged = {**result_in, "_execution_id": exec_id}
        assert merged["_execution_id"] == exec_id
        assert merged["query_purpose"] == "sales trend"
        assert merged["rows"] == result_in["rows"]
        assert merged["sql"] == result_in["sql"]
        assert merged["success"] is True

        # Persisted row + session-state.last metadata (optional but cheap).
        row = db.get(DataExecution, exec_id)
        assert row is not None
        assert row.tool_name == "ask_data_agent"
        assert row.session_id == "inject-sess-1"
        state = db.get(SessionState, "inject-sess-1")
        assert state is not None
        assert state.last_execution_id == exec_id
        assert state.last_tool_name == "ask_data_agent"
        assert state.execution_count >= 1
    finally:
        db.rollback()
        db.close()


@pytest.mark.asyncio
async def test_cache_skips_non_cacheable_tool_returns_none():
    """Non-cacheable tools return None -> the hook must NOT inject a key."""
    _ensure_table()
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        exec_id = await cache_data_execution(
            db=db,
            session_id="inject-sess-skip",
            tool_name="create_artifact",
            args={},
            result={"success": True},
            summary_text=None,
            org_id=None,
            app_id=None,
        )
        assert exec_id is None
    finally:
        db.rollback()
        db.close()


@pytest.mark.asyncio
async def test_set_last_metadata_with_org_app():
    """Verify SessionStateService.set_last persists org/app columns so the
    resume/intent-router flows can scope the cached execution."""
    _ensure_table()
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        state = SessionStateService.set_last(
            db,
            session_id="inject-sess-meta",
            execution_id="evt_inject_meta",
            tool_name="ask_data_agent",
            data_signature="sig_inject_1234abcd",
            org_id="org9",
            app_id="app9",
        )
        assert state is not None
        assert state.last_execution_id == "evt_inject_meta"
        assert state.last_data_signature == "sig_inject_1234abcd"
        assert state.org_id == "org9"
        assert state.app_id == "app9"
    finally:
        db.rollback()
        db.close()
