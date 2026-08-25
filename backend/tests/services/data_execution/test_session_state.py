"""Tests for SessionStateService (get_last / set_last / get_history).

Runs against the shared in-memory SQLite DB from ``tests/conftest.py``
(StaticPool), so the tests exercise the real SQLAlchemy mapper + real SQL.

Two fixtures make this work with the shared DB:

* ``_ensure_tables`` (module-scoped autouse): creates the app schema via
  ``Base.metadata.create_all``. It must run inside a fixture — not at import
  time — so the conftest session-scoped ``_ensure_shared_sqlite_engine`` has
  already swapped in the StaticPool engine.
* ``db`` teardown: deletes all ``session_states`` rows. The shared in-memory
  DB persists across tests, so without this cleanup the counter tests (which
  reuse the same ``session_id``) would see rows left by earlier tests.
"""

import pytest
from sqlalchemy import delete

from app.models.session_state import SessionState
from app.services.data_execution.session_state import SessionStateService


@pytest.fixture(scope="module", autouse=True)
def _ensure_tables():
    """Create the app schema on the shared in-memory test DB."""
    from app.database import Base, engine

    Base.metadata.create_all(bind=engine)


@pytest.fixture
def db():
    from app.database import SessionLocal

    session = SessionLocal()
    yield session
    session.execute(delete(SessionState))
    session.commit()
    session.close()


def test_set_last_creates_row(db):
    svc = SessionStateService
    result = svc.set_last(db, "sess-1")
    assert result is not None
    assert result.session_id == "sess-1"
    assert result.execution_count == 1


def test_set_last_upserts_on_same_session(db):
    svc = SessionStateService
    result = svc.set_last(db, "sess-1")
    assert result is not None
    result2 = svc.set_last(db, "sess-1")
    assert result2 is not None
    assert result2.execution_count == 2


def test_set_last_increments_counter(db):
    svc = SessionStateService
    result = svc.set_last(db, "sess-1")
    assert result is not None
    assert result.execution_count == 1
    db.commit()  # simulate the agent loop's explicit commit
    result2 = svc.set_last(db, "sess-1")
    assert result2 is not None
    assert result2.execution_count == 2


def test_get_last_returns_none_when_empty(db):
    svc = SessionStateService
    result = svc.get_last(db, "nonexistent")
    assert result is None


def test_get_history_returns_list(db):
    svc = SessionStateService
    result = svc.get_history(db, "sess-x")
    assert isinstance(result, list)
