"""Regression tests for C1, I3, M2, M3 entity-layer fixes."""
import os, uuid

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from datetime import datetime, timezone

import app.models  # noqa: F401
from app.database import Base
from app.models.agent_app import AgentApp


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _make_agent(db, agent_id, name, created_by):
    a = AgentApp(id=agent_id, name=name, created_by_id=created_by,
                 org_id="default-org", app_id="default-app",
                 capabilities=[], skills=[], is_system=False)
    db.add(a)
    db.commit()
    return a


# ── C1: owner scoping ──

def test_update_many_respects_owner_id(db):
    """C1: owner_id scoping prevents mass-IDOR."""
    from app.services.entity_service import update_many_records

    a1 = _make_agent(db, "agent-1", "Alice's Agent", "alice")
    a2 = _make_agent(db, "agent-2", "Bob's Agent", "bob")

    count = update_many_records(
        AgentApp, {}, {"$set": {"name": "HACKED"}}, db, owner_id="alice",
    )
    db.refresh(a1)
    db.refresh(a2)
    assert count == 1
    assert a1.name == "HACKED"
    assert a2.name == "Bob's Agent"  # NOT hacked


def test_update_many_filters_privileged_fields(db):
    """C1: $set routed through _filter_data — can't escalate privileges."""
    from app.services.entity_service import update_many_records

    a = _make_agent(db, "agent-priv", "X", "alice")

    update_many_records(
        AgentApp, {"id": "agent-priv"},
        {"$set": {"created_by_id": "evil", "org_id": "hacked-org"}},
        db, owner_id="alice",
    )
    db.refresh(a)
    assert a.created_by_id == "alice"          # NOT "evil"
    assert a.org_id == "default-org"            # NOT "hacked-org"


# ── I3: $push/$pull new list persistence ──

def test_push_operator_persists_new_list(db):
    """I3: $push builds new list so SQLAlchemy detects the change."""
    from app.services.entity_service import update_many_records

    a = _make_agent(db, "agent-arr", "X", "alice")
    assert (a.capabilities or []) == []

    update_many_records(
        AgentApp, {"id": "agent-arr"},
        {"$push": {"capabilities": ["forecasting"]}},
        db, owner_id="alice",
    )
    db.refresh(a)
    assert "forecasting" in (a.capabilities or [])

    update_many_records(
        AgentApp, {"id": "agent-arr"},
        {"$push": {"capabilities": ["rag"]}},
        db, owner_id="alice",
    )
    db.refresh(a)
    assert "forecasting" in (a.capabilities or [])
    assert "rag" in (a.capabilities or [])
    assert len(a.capabilities or []) == 2


def test_pull_operator_persists_new_list(db):
    """I3: $pull builds new list for persistence."""
    from app.services.entity_service import update_many_records

    a = _make_agent(db, "agent-pull", "X", "alice")
    a.capabilities = ["forecasting", "rag", "reporting"]
    db.commit()

    update_many_records(
        AgentApp, {"id": "agent-pull"},
        {"$pull": {"capabilities": "rag"}},
        db, owner_id="alice",
    )
    db.refresh(a)
    caps = a.capabilities or []
    assert "forecasting" in caps
    assert "rag" not in caps


# ── M2: secure default ──

def test_annotate_access_secure_default(db):
    """M2: can_edit defaults to False when owner_id is None."""
    from app.services.entity_service import _annotate_access

    _make_agent(db, "agent-m2", "X", "alice")
    annotated = _annotate_access(
        AgentApp, [{"id": "agent-m2", "name": "X"}],
        owner_id=None, db=db, is_admin=False,
    )
    assert annotated[0]["can_edit"] is False


# ── M3: fromisoformat ──

def test_coerce_for_model_timezone_offsets(db):
    """M3: _coerce_for_model handles Z, +08:00, and fractional seconds."""
    from app.services.entity_service import _coerce_for_model

    for s in ("2024-06-15T10:30:00Z", "2024-06-15T18:30:00+08:00",
              "2024-06-15T10:30:00.123456Z", "2024-06-15 10:30:00"):
        assert isinstance(_coerce_for_model(AgentApp, "created_date", s), datetime)

    assert _coerce_for_model(AgentApp, "created_date", "") is None
