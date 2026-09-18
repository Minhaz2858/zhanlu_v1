"""Phase 1d — plan / execute modes.

The primitive that lets ``turn_planner``'s server-authored plan be deleted.  The
mode does not author anything: it decides whether side-effecting tool calls run,
and points the model at its own task list (AgentTodo) as the plan.

What these tests pin:
  1. Mode resolution follows the same convention as permission_mode.
  2. Plan mode proposes instead of executing — and reads still run.
  3. A proposal is never retried (retrying burns the turn's iteration budget).
  4. The model is never trapped: mode_set / todo stay callable in plan mode.
  5. The plan read back out of AgentTodo is the model's, not the server's.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.harness.modes import (
    AGENT_MODE_KEY,
    DEFAULT_MODE,
    FALLBACK_EXTERNAL_TOOLS,
    get_mode,
    is_plan_mode,
    make_mode_patch,
    mode_to_system_block,
    normalize_mode,
    plan_from_todos,
    plan_mode_proposal,
    plan_progress,
    set_mode_on,
    tool_needs_execution,
)
from app.services.permissions.approval import NEVER_GATE


# ---------------------------------------------------------------------------
# Resolution — same convention as permission_mode
# ---------------------------------------------------------------------------

def test_default_mode_is_execute():
    assert get_mode(None) == "execute"
    assert get_mode({}) == "execute"
    assert get_mode({"foo": "bar"}) == DEFAULT_MODE


@pytest.mark.parametrize("value,expected", [
    ("plan", "plan"), ("PLAN", "plan"), (" execute ", "execute"),
    ("execute", "execute"), ("nonsense", "execute"), (None, "execute"),
    (42, "execute"), (["plan"], "execute"),
])
def test_normalize_mode(value, expected):
    assert normalize_mode(value) == expected


def test_mode_read_from_conversation_metadata():
    assert get_mode({AGENT_MODE_KEY: "plan"}) == "plan"
    assert is_plan_mode({AGENT_MODE_KEY: "plan"}) is True
    assert is_plan_mode({AGENT_MODE_KEY: "execute"}) is False


def test_mode_coexists_with_permission_mode():
    """The two settings share the metadata dict but are separate concepts."""
    meta = {"permission_mode": "full_auto", AGENT_MODE_KEY: "plan"}
    assert get_mode(meta) == "plan"


def test_set_mode_returns_a_copy():
    original = {"permission_mode": "default"}
    updated = set_mode_on(original, "plan")
    assert updated[AGENT_MODE_KEY] == "plan"
    assert updated["permission_mode"] == "default"
    # the caller's dict is untouched — SQLAlchemy needs reassignment to persist
    assert AGENT_MODE_KEY not in original


def test_make_mode_patch_shape():
    assert make_mode_patch("plan") == {AGENT_MODE_KEY: "plan"}
    assert make_mode_patch("junk") == {AGENT_MODE_KEY: "execute"}


# ---------------------------------------------------------------------------
# Enforcement: reads run, writes are proposed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("risk", ["read", "", "unknown"])
def test_reads_run_in_plan_mode(risk):
    assert tool_needs_execution(tool_name="ask_data_agent", risk=risk) is True


@pytest.mark.parametrize("risk", ["write", "destructive", "external"])
def test_side_effecting_calls_are_blocked_in_plan_mode(risk):
    assert tool_needs_execution(tool_name="create_agent", risk=risk) is False


@pytest.mark.parametrize("tool", ["todo", "memory", "mode_get", "mode_set"])
def test_scratch_state_stays_executable_in_plan_mode(tool):
    """The plan lives in the task list; blocking it would make plan mode unusable."""
    assert tool_needs_execution(tool_name=tool, risk="write", never_gate=True) is True


def test_known_message_senders_are_blocked_even_without_a_declared_risk():
    for tool in sorted(FALLBACK_EXTERNAL_TOOLS):
        assert tool_needs_execution(tool_name=tool, risk="") is False
    assert tool_needs_execution(tool_name="some_other_tool", risk="") is True


def test_proposal_shape():
    p = plan_mode_proposal(
        tool_name="kanban", arguments={"action": "delete", "card_id": "c1"},
        risk="destructive", action="delete",
    )
    assert p["success"] is False
    assert p["plan_mode"] is True
    assert p["retryable"] is False          # the loop must not retry this
    assert p["tool_name"] == "kanban"
    assert p["action"] == "delete"
    assert "NOT EXECUTED" in p["message"]
    assert "todo" in p["message"]           # tells the model where to put the plan


def test_proposal_has_no_error_key():
    """A proposal is not a failure — an 'error' field would read as one."""
    assert "error" not in plan_mode_proposal(tool_name="x", arguments={})


def test_proposal_is_not_retryable_by_the_loop_classifier():
    from app.services.agent_tools import _is_retryable
    p = plan_mode_proposal(tool_name="kanban", arguments={"action": "delete"})
    assert _is_retryable(p) is False
    # and neither is it considered a success
    assert p["success"] is False


# ---------------------------------------------------------------------------
# Enforcement through execute_tool (the real gate)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_tool_proposes_a_write_in_plan_mode():
    from app.services.agent_tools import execute_tool

    result = await execute_tool(
        "kanban", {"action": "delete", "card_id": "c1"},
        db=MagicMock(), user_id="u1",
        context={
            "agent_name": "general_assistant_probe",   # not a meta-agent
            "conversation_id": "conv-1",
            "conversation_metadata": {AGENT_MODE_KEY: "plan"},
        },
    )
    assert result.get("plan_mode") is True
    assert result["success"] is False


@pytest.mark.asyncio
async def test_execute_tool_does_not_propose_in_execute_mode():
    """Execute mode must not change today's behaviour."""
    from app.services.agent_tools import execute_tool

    with patch("app.services.agent_tools._CRUD_DISPATCH", {}), \
         patch("app.services.tool_registry.ToolRegistry.get_handler", return_value=None):
        result = await execute_tool(
            "no_such_tool_xyz", {},
            db=MagicMock(), user_id="u1",
            context={
                "agent_name": "general_assistant_probe",
                "conversation_metadata": {AGENT_MODE_KEY: "execute"},
            },
        )
    assert result.get("plan_mode") is None


def test_plan_mode_does_not_override_an_authorization_deny():
    """Ordering: a hard deny wins over plan mode's proposal."""
    from app.services.agent_tools import _is_retryable  # noqa: F401  (import sanity)
    from app.services.permissions import PermissionChecker, PermissionConfig

    checker = PermissionChecker(PermissionConfig(denied_tools=["kanban"]))
    result = checker.check_tool(
        "kanban", {"action": "delete"},
        conversation_metadata={AGENT_MODE_KEY: "plan"},
    )
    assert result.allowed is False


# ---------------------------------------------------------------------------
# The plan is the model's own task list
# ---------------------------------------------------------------------------

def test_plan_read_back_from_todos():
    row = MagicMock()
    row.items = [
        {"id": "1", "content": "Check the schema", "status": "completed"},
        {"id": "2", "content": "Query August contracts", "status": "in_progress"},
        {"id": "3", "content": "Build the deck", "status": "pending"},
    ]
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = row

    steps = plan_from_todos(db, "conv-1")
    assert [s["title"] for s in steps] == [
        "Check the schema", "Query August contracts", "Build the deck",
    ]
    assert plan_progress(steps) == {
        "total": 3, "completed": 1, "in_progress": 1, "pending": 1,
    }


def test_plan_from_todos_never_raises():
    db = MagicMock()
    db.query.side_effect = RuntimeError("db down")
    assert plan_from_todos(db, "conv-1") == []
    assert plan_from_todos(db, "") == []
    assert plan_from_todos(None, "conv-1") == []


def test_plan_from_todos_tolerates_junk_items():
    row = MagicMock()
    row.items = [None, 42, {"content": "ok"}, {"title": "titled"}]
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = row
    steps = plan_from_todos(db, "conv-1")
    assert len(steps) == 2
    assert steps[0]["title"] == "ok"
    assert steps[1]["title"] == "titled"


def test_plan_progress_ignores_unknown_status():
    assert plan_progress([{"status": "weird"}])["total"] == 1
    assert plan_progress([])["total"] == 0


# ---------------------------------------------------------------------------
# Never trapped in plan mode
# ---------------------------------------------------------------------------

def test_mode_tools_are_never_gated():
    assert "mode_get" in NEVER_GATE
    assert "mode_set" in NEVER_GATE


def test_mode_tools_stay_executable_in_plan_mode():
    from app.services.harness.modes import tool_needs_execution
    for tool in ("mode_get", "mode_set"):
        assert tool_needs_execution(tool_name=tool, risk="read", never_gate=True) is True


def test_system_block_teaches_the_mode():
    plan_block = mode_to_system_block("plan")
    assert "PLAN" in plan_block
    assert "todo" in plan_block
    assert "mode_set" in plan_block
    assert "PLAN" not in mode_to_system_block("execute")


# ---------------------------------------------------------------------------
# mode_get / mode_set handlers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mode_get_reports_mode_and_plan():
    from app.services.tool_handlers.mode_tool import _mode_get

    with patch("app.services.tool_handlers.mode_tool.plan_from_todos",
               return_value=[{"id": "1", "title": "step", "status": "pending"}]):
        result = await _mode_get(
            {}, db=MagicMock(), user_id="u1",
            context={"conversation_id": "conv-1", "conversation_metadata": {AGENT_MODE_KEY: "plan"}},
        )
    assert result["success"] is True
    assert result["mode"] == "plan"
    assert result["plan_progress"]["total"] == 1


@pytest.mark.asyncio
async def test_mode_set_rejects_an_invalid_mode():
    from app.services.tool_handlers.mode_tool import _mode_set

    result = await _mode_set(
        {"mode": "sideways"}, db=MagicMock(), user_id="u1",
        context={"conversation_id": "conv-1"},
    )
    assert result["success"] is False
    assert "must be one of" in result["error"]


@pytest.mark.asyncio
async def test_mode_set_persists_to_conversation_metadata():
    from app.services.tool_handlers.mode_tool import _mode_set

    conv = MagicMock()
    conv.metadata_ = {"permission_mode": "default"}
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = conv

    result = await _mode_set(
        {"mode": "plan"}, db=db, user_id="u1",
        context={"conversation_id": "conv-1"},
    )
    assert result["success"] is True
    assert conv.metadata_[AGENT_MODE_KEY] == "plan"
    assert conv.metadata_["permission_mode"] == "default"   # other keys preserved
    db.commit.assert_called()


@pytest.mark.asyncio
async def test_mode_set_without_a_conversation_is_a_clean_error():
    from app.services.tool_handlers.mode_tool import _mode_set

    result = await _mode_set({"mode": "plan"}, db=MagicMock(), user_id="u1", context={})
    assert result["success"] is False
    assert "conversation" in result["error"].lower()


@pytest.mark.asyncio
async def test_mode_set_survives_a_db_failure():
    from app.services.tool_handlers.mode_tool import _mode_set

    db = MagicMock()
    db.query.side_effect = RuntimeError("db down")
    result = await _mode_set(
        {"mode": "plan"}, db=db, user_id="u1",
        context={"conversation_id": "conv-1"},
    )
    assert result["success"] is False
    assert "Could not set mode" in result["error"]
