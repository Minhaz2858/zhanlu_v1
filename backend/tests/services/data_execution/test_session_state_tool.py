"""Tests for session_state_query tool (exposes recent data_execution history).

Runs against the shared in-memory SQLite DB from ``tests/conftest.py``
(StaticPool). Unique session_ids per test avoid state bleed on the shared DB.

NOTE (adapted from plan/brief): ``get_history`` reads the ``data_executions``
table, NOT ``session_states``. Tests therefore seed ``DataExecution`` rows
directly with explicit distinct ``created_date`` values so the newest-first
ORDER BY assertion is deterministic. ``set_last`` is not involved.
"""
from datetime import datetime, timezone

import pytest

from app.models.data_execution import DataExecution
from app.services.data_execution.session_state_tool import (
    SESSION_STATE_QUERY_SCHEMA,
    session_state_query,
)


@pytest.fixture(scope="module", autouse=True)
def _ensure_tables():
    """Create the app schema on the shared in-memory test DB."""
    from app.database import Base, engine

    Base.metadata.create_all(bind=engine)


def _seed_execution(db, *, exec_id, session_id, tool_name, created_date):
    """Insert one data_executions row with an explicit id + timestamp."""
    db.add(
        DataExecution(
            id=exec_id,
            session_id=session_id,
            tool_name=tool_name,
            args={},
            result={},
            summary_text="summary for " + tool_name,
            created_date=created_date,
            org_id=None,
            app_id=None,
            is_deleted=False,
        )
    )
    db.commit()


def test_schema_has_name_and_description():
    assert SESSION_STATE_QUERY_SCHEMA["type"] == "function"
    assert SESSION_STATE_QUERY_SCHEMA["function"]["name"] == "session_state_query"
    assert "previous" in SESSION_STATE_QUERY_SCHEMA["function"]["description"].lower()


def test_session_state_query_returns_seeded_history():
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        base = datetime(2026, 8, 25, 1, 0, 0, tzinfo=timezone.utc)
        _seed_execution(
            db, exec_id="exec-1", session_id="t6-history-1",
            tool_name="ask_data_agent", created_date=base,
        )
        _seed_execution(
            db, exec_id="exec-2", session_id="t6-history-1",
            tool_name="query_composer", created_date=base.replace(minute=1),
        )

        result = session_state_query(db, session_id="t6-history-1")
        assert len(result) == 2
        # newest first
        assert result[0]["execution_id"] == "exec-2"
        assert result[1]["execution_id"] == "exec-1"
        assert result[0]["tool_name"] == "query_composer"
    finally:
        db.rollback()
        db.close()


def test_session_state_query_respects_limit():
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        base = datetime(2026, 8, 25, 2, 0, 0, tzinfo=timezone.utc)
        for i in range(3):
            _seed_execution(
                db, exec_id=f"lim-{i}", session_id="t6-limit-1",
                tool_name="ask_data_agent", created_date=base.replace(minute=i),
            )

        result = session_state_query(db, session_id="t6-limit-1", limit=2)
        assert len(result) == 2
        assert result[0]["execution_id"] == "lim-2"
        assert result[1]["execution_id"] == "lim-1"
    finally:
        db.rollback()
        db.close()


def test_session_state_query_unknown_session_empty():
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        result = session_state_query(db, session_id="t6-no-such-session")
        assert result == []
    finally:
        db.rollback()
        db.close()
