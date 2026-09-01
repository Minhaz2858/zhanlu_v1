"""Tests for the auto-cache service (cache.py) + SessionStateService.set_last metadata.

Runs against the shared in-memory SQLite DB from ``tests/conftest.py``
(StaticPool). ``_ensure_table()`` creates just the tables these tests need
(repo convention — see test_data_execution_model.py / test_session_state_model.py).

The shared DB persists across tests, so each test uses a unique session_id
to avoid counter bleed.
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import pytest

from app.database import engine
from app.models.data_execution import DataExecution
from app.models.session_state import SessionState
from app.services.data_execution.cache import (
    CACHEABLE_TOOL_PATTERNS,
    cache_data_execution,
)
from app.services.data_execution.session_state import SessionStateService


def _ensure_table():
    """Create just the data_executions + session_states tables (idempotent)."""
    DataExecution.__table__.create(bind=engine, checkfirst=True)
    SessionState.__table__.create(bind=engine, checkfirst=True)


def test_cacheable_tool_patterns_contains_main_data_tools():
    assert "ask_data_agent" in CACHEABLE_TOOL_PATTERNS
    assert "ask_erp_kpi" in CACHEABLE_TOOL_PATTERNS
    assert "query_composer" in CACHEABLE_TOOL_PATTERNS
    assert "fetch_data_batch" in CACHEABLE_TOOL_PATTERNS
    assert "create_artifact" not in CACHEABLE_TOOL_PATTERNS


@pytest.mark.asyncio
async def test_cache_data_execution_returns_none_for_non_cacheable_tool():
    result = await cache_data_execution(
        db=None,
        session_id="s1",
        tool_name="create_artifact",
        args={},
        result={},
        summary_text=None,
        org_id=None,
        app_id=None,
    )
    assert result is None


@pytest.mark.asyncio
async def test_cache_data_execution_persists_row():
    _ensure_table()
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        exec_id = await cache_data_execution(
            db=db,
            session_id="cache-sess-1",
            tool_name="ask_data_agent",
            args={"query": "test"},
            result={"rows": [{"a": 1}], "columns": ["a"], "sql": "SELECT 1"},
            summary_text="test summary",
            org_id="org1",
            app_id="default-app",
        )
        assert exec_id is not None
        assert exec_id.startswith("evt_")
        row = db.get(DataExecution, exec_id)
        assert row is not None
        assert row.tool_name == "ask_data_agent"
        state = db.get(SessionState, "cache-sess-1")
        assert state is not None
        assert state.last_execution_id == exec_id
        assert state.last_tool_name == "ask_data_agent"
        assert state.execution_count >= 1
    finally:
        db.rollback()
        db.close()


@pytest.mark.asyncio
async def test_cache_data_execution_is_best_effort_on_db_error():
    _ensure_table()
    from unittest.mock import patch

    from app.database import SessionLocal

    # Repo convention (see test_data_execution_model.py): inject a DB failure
    # by making the DataExecution constructor raise. A real "closed session"
    # does NOT fail in SQLAlchemy 2.x — a closed Session is still reusable
    # and starts a fresh transaction on next use.
    db = SessionLocal()
    try:
        with patch(
            "app.services.data_execution.cache.DataExecution"
        ) as mock_model:
            mock_model.side_effect = RuntimeError("db down")
            result = await cache_data_execution(
                db=db,
                session_id="cache-sess-fail",
                tool_name="ask_data_agent",
                args={},
                result={},
                summary_text=None,
                org_id=None,
                app_id=None,
            )
            assert result is None
    finally:
        db.rollback()
        db.close()


@pytest.mark.asyncio
async def test_repeated_execution_same_session_gets_distinct_ids():
    """Two cache writes with identical data must produce distinct PKs.

    Regression: a deterministic id (sha1(session|tool|signature)) collides when
    the same query re-runs in the same session -> PK IntegrityError -> second
    execution silently dropped + set_last never updated -> stale re-export.
    """
    _ensure_table()
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        kwargs = dict(
            db=db,
            session_id="cache-sess-rerun",
            tool_name="ask_data_agent",
            args={"query": "same"},
            result={"rows": [{"a": 1}], "columns": ["a"], "sql": "SELECT 1"},
            summary_text="run",
            org_id=None,
            app_id=None,
        )
        first = await cache_data_execution(**kwargs)
        second = await cache_data_execution(**kwargs)
        assert first is not None and second is not None
        assert first != second
        assert db.get(DataExecution, first) is not None
        assert db.get(DataExecution, second) is not None
        # session state points at the LATEST execution
        state = db.get(SessionState, "cache-sess-rerun")
        assert state.last_execution_id == second
        assert state.execution_count == 2
    finally:
        db.rollback()
        db.close()


def test_set_last_persists_metadata_columns():
    _ensure_table()
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        state = SessionStateService.set_last(
            db,
            session_id="cache-sess-meta",
            execution_id="evt_meta123",
            tool_name="ask_data_agent",
            data_signature="sig1234567890abcd",
            org_id="org1",
            app_id="default-app",
        )
        assert state is not None
        assert state.last_execution_id == "evt_meta123"
        assert state.last_tool_name == "ask_data_agent"
        assert state.last_data_signature == "sig1234567890abcd"
        assert state.org_id == "org1"
        assert state.app_id == "default-app"
    finally:
        db.rollback()
        db.close()
