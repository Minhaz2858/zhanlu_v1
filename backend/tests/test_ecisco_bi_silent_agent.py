"""Regression: ecisco_bi_assistant is a silent platform system agent.

Why this exists
---------------
After the EDIA migration, ecisco_bi_assistant became a platform-critical
orchestrator that frontend/src/pages/Chat.jsx auto-selects silently when
the user opens a chat in the "Ecisco BI" project. End users must never
see this agent in any UI surface (My Space, Company Agents, agent picker),
and it must be undeletable through any code path. These tests guard the
contract:

  1. The seeder marks ecisco_bi_assistant as is_system=True.
  2. ensure_system_agents() back-fills is_system=True on legacy rows.
  3. soft_delete_record() refuses to delete a system agent.
  4. delete_many_records() excludes system agents from bulk deletion.
  5. soft_delete_record() still works for non-system agents.
  6. ensure_system_agents() is idempotent w.r.t. is_system for the agent.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.agent_app import AgentApp
from app.services import entity_service
from app.services.system_agents import ensure_system_agents


@pytest.fixture
def db():
    """Fresh in-memory SQLite for each test, with all tables created."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


# --- 1. Seeder marks ecisco_bi_assistant as is_system=True ---


def test_ecisco_bi_assistant_is_marked_system_in_seeder(db):
    """After seeding, ecisco_bi_assistant must have is_system=True so
    the frontend's filterUserAgents() / isSystemAgent() hide it from
    every user-facing list."""
    ensure_system_agents(db)
    row = db.query(AgentApp).filter(
        AgentApp.name == "ecisco_bi_assistant"
    ).first()
    assert row is not None, "ensure_system_agents did not create ecisco_bi_assistant"
    assert row.is_system is True, (
        f"ecisco_bi_assistant should be is_system=True, "
        f"got is_system={row.is_system}"
    )


# --- 2. ensure_system_agents() back-fills is_system=True on legacy rows ---


def test_ensure_system_agents_backfills_legacy_ecisco_bi_assistant(db):
    """A row that exists from before this change was made must be
    promoted to is_system=True on the next ensure_system_agents() call.
    Without this backfill, the frontend would still surface the agent
    in Company Agents / My Space because is_system would remain False."""
    legacy = AgentApp(
        name="ecisco_bi_assistant",
        description="legacy pre-migration row",
        is_system=False,  # the bug case
    )
    db.add(legacy)
    db.commit()

    ensure_system_agents(db)

    refreshed = db.query(AgentApp).filter(
        AgentApp.name == "ecisco_bi_assistant"
    ).first()
    assert refreshed is not None
    assert refreshed.is_system is True, (
        "Legacy ecisco_bi_assistant row was not promoted to is_system=True"
    )


# --- 3. soft_delete_record() refuses system agents ---


def test_soft_delete_record_refuses_system_agent(db):
    """A row with is_system=True must NOT be soft-deletable through the
    standard entity_service path. The guard returns False so the router
    can translate it to HTTP 404, and the row must remain is_deleted=False."""
    # Seed the agent via the same code path the seeder uses.
    ensure_system_agents(db)
    agent = db.query(AgentApp).filter(
        AgentApp.name == "ecisco_bi_assistant"
    ).first()
    assert agent.is_system is True, "precondition: agent must be system"

    # soft_delete_record returns False instead of deleting.
    success = entity_service.soft_delete_record(AgentApp, agent.id, db)
    assert success is False, (
        "soft_delete_record returned True for a system agent; the guard "
        "should have refused and returned False"
    )
    db.refresh(agent)
    assert agent.is_deleted is False, (
        "system agent was soft-deleted despite is_system=True"
    )


# --- 4. delete_many_records() excludes system agents from bulk deletion ---


def test_delete_many_records_skips_system_agents(db):
    """A bulk-delete with a query that matches the system agent must
    exclude that agent — the count of deleted rows must not include
    it, and the row must remain is_deleted=False after the call."""
    ensure_system_agents(db)
    user_agent = AgentApp(
        name="User Agent",
        description="not a system agent",
        is_system=False,
    )
    db.add(user_agent)
    db.commit()

    # Bulk-delete: match all non-deleted AgentApp rows.
    count = entity_service.delete_many_records(
        AgentApp, {}, db
    )
    # Only the user agent is deleted; the system agent is excluded.
    assert count == 1, f"expected 1 deletion (user agent), got {count}"

    system_row = db.query(AgentApp).filter(
        AgentApp.name == "ecisco_bi_assistant"
    ).first()
    assert system_row.is_deleted is False, (
        "ecisco_bi_assistant was bulk-deleted despite is_system=True"
    )


# --- 5. soft_delete_record() still works for non-system agents ---


def test_soft_delete_record_works_for_non_system_agent(db):
    """Regression guard: the new is_system guard must not block deletion
    of normal user-created agents. A row with is_system=False is still
    soft-deletable as before."""
    user_agent = AgentApp(
        name="User Agent",
        description="not a system agent",
        is_system=False,
    )
    db.add(user_agent)
    db.commit()

    success = entity_service.soft_delete_record(AgentApp, user_agent.id, db)
    assert success is True, "soft_delete_record should still delete user agents"
    db.refresh(user_agent)
    assert user_agent.is_deleted is True


# --- 6. ensure_system_agents() is idempotent for ecisco_bi_assistant ---


def test_ensure_system_agents_idempotent_for_ecisco_bi(db):
    """Running ensure_system_agents() twice must keep ecisco_bi_assistant
    at is_system=True and must not create a second row."""
    ensure_system_agents(db)
    ensure_system_agents(db)

    rows = db.query(AgentApp).filter(
        AgentApp.name == "ecisco_bi_assistant"
    ).all()
    assert len(rows) == 1, (
        f"ensure_system_agents created {len(rows)} ecisco_bi_assistant rows, "
        f"expected exactly 1"
    )
    assert rows[0].is_system is True
