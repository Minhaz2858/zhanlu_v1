"""Test the SessionState model.

Mirrors the flat test layout of ``test_data_execution_model.py`` (Task 1).
These tests only introspect the mapper/table — no DB writes required.
"""

from app.models.session_state import SessionState


def test_session_state_model_imports():
    assert SessionState.__tablename__ == "session_states"


def test_session_state_session_id_is_primary_key():
    col = SessionState.__table__.columns["session_id"]
    assert col.primary_key is True
    assert col.type.python_type is str


def test_session_state_has_required_fields():
    fields = {c.name for c in SessionState.__table__.columns}
    assert fields >= {
        "session_id", "last_execution_id", "last_tool_name",
        "last_data_signature", "execution_count", "org_id", "app_id",
        "is_deleted", "created_date", "updated_date",
    }


def test_session_state_primary_key_is_only_session_id():
    """WATCH ITEM regression: id must NOT join the primary key (composite
    PK would mismatch the migration). session_id is the single PK."""
    pk = SessionState.__table__.primary_key
    assert {c.name for c in pk} == {"session_id"}
