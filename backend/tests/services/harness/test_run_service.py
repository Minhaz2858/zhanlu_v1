"""Tests for AgentRunService — inline/queued modes, collect, drain."""
from __future__ import annotations

import pytest

from app.services.harness.run_service import AgentRunService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def svc():
    return AgentRunService()


# ---------------------------------------------------------------------------
# create_run_record
# ---------------------------------------------------------------------------

def test_create_run_record_best_effort(svc):
    """create_run_record returns a run_id even when DB isn't connected."""
    rid = svc.create_run_record(
        agent_name="test_agent",
        task="hello",
        mode="inline",
        caller_context={"org_id": "default-org", "app_id": "default-app"},
    )
    assert isinstance(rid, str)
    assert len(rid) == 32


def test_create_run_record_with_pre_assigned_id(svc):
    rid = svc.create_run_record(
        agent_name="test_agent",
        task="hello",
        mode="queued",
        run_id="abc123abc123abc123abc123abc123ab",
    )
    assert rid == "abc123abc123abc123abc123abc123ab"


# ---------------------------------------------------------------------------
# get_run
# ---------------------------------------------------------------------------

def test_get_run_returns_none_for_unknown(svc):
    assert svc.get_run("nonexistent_run_id") is None
