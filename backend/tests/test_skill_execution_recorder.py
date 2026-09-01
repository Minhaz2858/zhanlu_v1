"""Test that SkillExecutionRecorder correctly inserts SkillRun records.

Verifies that the recorder:
1. Inserts a SkillRun row with the correct fields
2. Is non-blocking (doesn't raise on DB errors)
3. Extracts conversation_id and agent_name from context
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import pytest

from app.database import Base, engine, SessionLocal
import app.models  # noqa: F401

Base.metadata.create_all(engine)


def test_recorder_inserts_skill_run():
    """SkillExecutionRecorder.record() should insert a SkillRun row."""
    from app.services.skill_execution_recorder import SkillExecutionRecorder
    from app.models.skill_run import SkillRun

    SkillExecutionRecorder.record(
        skill_name="test-recorder-skill",
        action="load",
        status="completed",
        conversation_id="conv-123",
        agent_name="skill_agent",
        duration_ms=42,
    )

    db = SessionLocal()
    try:
        runs = db.query(SkillRun).filter(
            SkillRun.conversation_id == "conv-123"
        ).all()
        assert len(runs) >= 1
        run = runs[-1]
        assert run.status == "completed"
        assert run.duration_ms == 42
        assert run.input_json["skill_name"] == "test-recorder-skill"
        assert run.input_json["agent_name"] == "skill_agent"
        assert run.input_json["action"] == "load"
    finally:
        db.close()


def test_recorder_record_from_context():
    """record_from_context should extract conversation_id and agent_name from context."""
    from app.services.skill_execution_recorder import SkillExecutionRecorder
    from app.models.skill_run import SkillRun

    SkillExecutionRecorder.record_from_context(
        skill_name="test-context-skill",
        action="execute",
        status="failed",
        context={"conversation_id": "conv-ctx-456", "agent_name": "general_assistant"},
        error_message="Skill not found",
    )

    db = SessionLocal()
    try:
        run = db.query(SkillRun).filter(
            SkillRun.conversation_id == "conv-ctx-456"
        ).order_by(SkillRun.created_date.desc()).first()
        assert run is not None
        assert run.status == "failed"
        assert run.error_message == "Skill not found"
        assert run.input_json["skill_name"] == "test-context-skill"
        assert run.input_json["agent_name"] == "general_assistant"
    finally:
        db.close()


def test_recorder_non_blocking_on_error():
    """If the DB is unavailable, the recorder should not raise."""
    from app.services.skill_execution_recorder import SkillExecutionRecorder
    from unittest.mock import patch

    # Force a DB error by making SessionLocal raise
    with patch("app.database.SessionLocal", side_effect=Exception("DB unavailable")):
        # This should NOT raise
        SkillExecutionRecorder.record(
            skill_name="test-error-skill",
            action="load",
            status="completed",
        )


def test_recorder_handles_none_context():
    """record_from_context should handle None context gracefully."""
    from app.services.skill_execution_recorder import SkillExecutionRecorder
    from app.models.skill_run import SkillRun

    # This should not raise even with None context
    SkillExecutionRecorder.record_from_context(
        skill_name="test-none-ctx-skill",
        action="load",
        status="completed",
        context=None,
    )

    db = SessionLocal()
    try:
        run = db.query(SkillRun).filter(
            SkillRun.input_json["skill_name"].as_string() == "test-none-ctx-skill"
        ).first()
        assert run is not None
        assert run.conversation_id is None
    finally:
        db.close()
