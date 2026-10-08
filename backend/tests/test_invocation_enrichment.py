"""Tests for agent_invocations route/outcome enrichment (IBM workstream E1)."""
from unittest.mock import MagicMock

import pytest

from app.services.agent_invocations import record_invocation


@pytest.fixture
def db():
    """In-memory-ish session mock: capture added rows instead of hitting DB."""
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = MagicMock(
        id="app-123")  # _resolve_agent_app_id hit
    rows = []
    orig_add = session.add

    def _add(row):
        rows.append(row)
        row.id = "row-1"

    session.add = _add
    session.rows = rows
    return session


def test_record_invocation_persists_route_outcome(db):
    row_id = record_invocation(
        db,
        agent_app_id="app-123",
        conversation_id="conv-1",
        invocation_type="conversation",
        status="completed",
        route="react",
        outcome="completed",
        tools_selected=17,
        delegation_mode="swarm",
    )
    assert row_id == "row-1"
    row = db.rows[0]
    assert row.route == "react"
    assert row.outcome == "completed"
    assert row.tools_selected == 17
    assert row.delegation_mode == "swarm"


def test_record_invocation_route_optional(db):
    row_id = record_invocation(db, agent_app_id="app-123")
    row = db.rows[0]
    assert row_id == "row-1"
    assert row.route is None
    assert row.outcome is None
    assert row.tools_selected is None
    assert row.delegation_mode is None


def test_record_invocation_trims_long_values(db):
    record_invocation(
        db, agent_app_id="app-123",
        route="x" * 100, outcome="y" * 100, delegation_mode="z" * 50,
    )
    row = db.rows[0]
    assert len(row.route) == 32
    assert len(row.outcome) == 32
    assert len(row.delegation_mode) == 16


def test_record_invocation_failure_is_non_fatal():
    from unittest.mock import patch

    bad_db = MagicMock()
    bad_db.query.side_effect = RuntimeError("db down")
    with patch("app.services.agent_invocations.logger"):
        result = record_invocation(bad_db, agent_app_id="app-123", route="react")
    assert result is None  # never raises


def _agents_source():
    import inspect
    from app.routers import agents
    return inspect.getsource(agents)


def test_router_records_all_four_routes():
    src = _agents_source()
    assert 'route="react"' in src
    assert 'route="fsm"' in src
    assert 'route="cache"' in src
    assert 'route="workflow"' in src
