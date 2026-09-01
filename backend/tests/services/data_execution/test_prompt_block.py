"""Tests for build_session_state_block (prompt injection of cached execution state).

Runs against the shared in-memory SQLite DB from ``tests/conftest.py``
(StaticPool). Unique session_ids per test avoid state bleed on the shared DB.
"""
import pytest

from app.services.data_execution.prompt_block import build_session_state_block
from app.services.data_execution.session_state import SessionStateService


@pytest.fixture(scope="module", autouse=True)
def _ensure_tables():
    """Create the app schema on the shared in-memory test DB."""
    from app.database import Base, engine

    Base.metadata.create_all(bind=engine)


def test_returns_none_when_no_execution():
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        result = build_session_state_block(db, session_id="test-no-exec")
        assert result is None
    finally:
        db.close()


def test_returns_block_with_execution_id():
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        SessionStateService.set_last(
            db, session_id="test-block-1",
            execution_id="evt_abc123", tool_name="ask_data_agent",
            data_signature="abc", org_id=None, app_id=None,
        )
        result = build_session_state_block(db, session_id="test-block-1")
        assert result is not None
        assert "evt_abc123" in result
        assert "ask_data_agent" in result
        assert "create_artifact" in result
    finally:
        db.rollback()
        db.close()
