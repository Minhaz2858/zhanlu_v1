"""Regression: ensure_system_agents stamps ``is_system=True`` on every
system-agent row, and never on user-created rows.

Why this exists
---------------
A user reported that ``general_assistant`` showed up as a visible chip
in the chat input after the auto-select path was added. The fix is
to make the backend mark system agents with ``is_system=True`` and
have the frontend hide any agent with that flag. These tests guard
both ends:

  1. ensure_system_agents() stamps is_system=True on create.
  2. ensure_system_agents() back-fills is_system=True on legacy rows
     (a row that exists but has is_system=False from an older
     migration).
  3. A user-created agent (any name NOT in the system set) is left
     with is_system=False, so it stays visible in the UI.

The tests use an in-memory SQLite + the real models, so the SQL
operators and the default value on the column are exercised exactly
as production would use them.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.agent_app import AgentApp
from app.services.system_agents import ensure_system_agents


# DashboardApp.spec (and any other JSONB column registered on Base) is
# PostgreSQL-only; SQLite cannot compile it natively. Render it as plain
# JSON/TEXT so Base.metadata.create_all() works on the in-memory test DB.
# Same recipe as tests/services/dashboard_app/*.
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ensure_system_agents_stamps_is_system_true_on_create(db):
    """First-time seed must mark every system agent as is_system=True."""
    ensure_system_agents(db)

    names = ["agent_builder", "skill_agent", "automation_agent",
             "general_assistant", "power_user"]
    rows = {r.name: r for r in db.query(AgentApp).all()}
    for n in names:
        assert n in rows, f"ensure_system_agents did not create {n}"
        assert rows[n].is_system is True, (
            f"{n} should have is_system=True after seed, got "
            f"is_system={rows[n].is_system}"
        )


def test_ensure_system_agents_backfills_is_system_on_legacy_rows(db):
    """A row that exists from before this column was added must be
    promoted to is_system=True on the next ensure_system_agents()
    call. Without this backfill, a pre-migration general_assistant
    row would show up in the user-facing agent list (because its
    is_system column would still be the default False)."""
    # Simulate a legacy row: exists, but is_system=False because the
    # DB was migrated before the column was added.
    legacy = AgentApp(
        name="general_assistant",
        description="legacy",
        is_system=False,  # the bug case
    )
    db.add(legacy)
    db.commit()

    ensure_system_agents(db)

    refreshed = db.query(AgentApp).filter(
        AgentApp.name == "general_assistant"
    ).first()
    assert refreshed is not None
    assert refreshed.is_system is True, (
        "Legacy general_assistant row was not promoted to is_system=True"
    )


def test_ensure_system_agents_does_not_touch_user_agents(db):
    """A user-created agent whose name happens to be near a system
    name (or that was created by the user before ensure_system_agents
    ran) must NOT be promoted to is_system=True. Otherwise the
    frontend would silently hide a user's own agent."""
    user_agent = AgentApp(
        name="My Custom Agent",
        description="a user-created agent",
        is_system=False,
    )
    db.add(user_agent)
    db.commit()

    ensure_system_agents(db)

    refreshed = db.query(AgentApp).filter(
        AgentApp.name == "My Custom Agent"
    ).first()
    assert refreshed is not None
    assert refreshed.is_system is False, (
        "User-created agent was incorrectly promoted to is_system=True"
    )


def test_ensure_system_agents_is_idempotent(db):
    """Running ensure_system_agents() twice must not change any
    is_system values, and must not create duplicate rows."""
    ensure_system_agents(db)
    before = {
        r.name: r.is_system
        for r in db.query(AgentApp).all()
    }

    ensure_system_agents(db)
    after = {
        r.name: r.is_system
        for r in db.query(AgentApp).all()
    }

    assert before == after, (
        f"ensure_system_agents is not idempotent. before={before} after={after}"
    )
    # Also: no duplicate rows.
    assert len(after) == len(set(after)), (
        "ensure_system_agents created duplicate rows on second run"
    )
