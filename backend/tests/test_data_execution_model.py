"""Tests for the DataExecution model + persist_data_execution service.

Uses the repo convention (see test_dashboard_model.py): force a SQLite
DATABASE_URL before importing app modules so the module-level engine in
``app.database`` binds to SQLite. Under pytest the ``tests/conftest.py``
already sets a shared in-memory SQLite URL, so ``setdefault`` is a no-op
there; the file-based fallback only matters when the file is executed
directly.
"""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

from app.database import SessionLocal, engine
from app.models.base import TimestampedBase
from app.models.data_execution import DataExecution
from app.services.data_execution.persist import persist_data_execution


def _ensure_table():
    """Create just the data_executions table (idempotent) in the test DB."""
    DataExecution.__table__.create(bind=engine, checkfirst=True)


def test_data_execution_inherits_timestamped_base():
    assert issubclass(DataExecution, TimestampedBase)


def test_data_execution_has_required_columns():
    cols = DataExecution.__table__.c
    for name in (
        "id",
        "session_id",
        "tool_name",
        "args",
        "result",
        "summary_text",
        "expires_at",
        "org_id",
        "app_id",
        "is_deleted",
        "created_date",
        "updated_date",
        "created_by_id",
    ):
        assert name in cols, f"missing column {name}"
    assert cols.session_id.nullable is False
    assert cols.tool_name.nullable is False
    assert cols.args.nullable is False
    assert cols.result.nullable is False
    assert cols.summary_text.nullable is True
    assert cols.expires_at.nullable is True
    assert str(cols.id.type.length) == "64"


def test_data_execution_tablename():
    assert DataExecution.__tablename__ == "data_executions"


def test_data_execution_is_expired():
    now = datetime.now(timezone.utc)
    past = DataExecution(expires_at=now - timedelta(seconds=1))
    future = DataExecution(expires_at=now + timedelta(seconds=3600))
    none_exp = DataExecution(expires_at=None)
    assert past.is_expired() is True
    assert future.is_expired() is False
    assert none_exp.is_expired() is False
    # Naive datetimes (SQLite round-trips) are treated as UTC.
    naive_past = DataExecution(expires_at=(now - timedelta(seconds=1)).replace(tzinfo=None))
    assert naive_past.is_expired() is True
    # Explicit `now` argument.
    assert future.is_expired(now=now + timedelta(seconds=7200)) is True


def test_persist_data_execution_persists_row():
    _ensure_table()
    now = datetime.now(timezone.utc)
    row = persist_data_execution(
        "sess-1",
        "ask_data_agent",
        args={"question": "q1"},
        result={"rows": [{"a": 1}], "count": 1},
    )
    assert row is not None
    assert row.id.startswith("evt_")
    assert row.session_id == "sess-1"
    assert row.tool_name == "ask_data_agent"
    assert row.args == {"question": "q1"}
    assert row.result == {"rows": [{"a": 1}], "count": 1}
    # Default args/result when omitted.
    row2 = persist_data_execution("sess-2", "ask_data_agent")
    assert row2 is not None
    assert row2.args == {}
    assert row2.result == {}

    # Query the row back through a fresh session.
    with SessionLocal() as db:
        loaded = db.get(DataExecution, row.id)
    assert loaded is not None
    assert loaded.session_id == "sess-1"
    assert loaded.tool_name == "ask_data_agent"
    assert loaded.args == {"question": "q1"}
    assert loaded.result == {"rows": [{"a": 1}], "count": 1}
    # expires_at ≈ now + 3600s (naive on SQLite → treat as UTC).
    exp = loaded.expires_at
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    assert exp is not None
    assert abs((exp - now).total_seconds() - 3600) < 60

    # Clean up the file-based fallback DB if it was created.
    if os.path.exists("./test_runtime.db"):
        os.remove("./test_runtime.db")


@patch("app.services.data_execution.persist.SessionLocal")
def test_persist_data_execution_returns_none_on_db_error(mock_session_local):
    mock_session_local.side_effect = RuntimeError("db down")
    result = persist_data_execution("sess-x", "ask_data_agent", args={"q": 1})
    assert result is None
