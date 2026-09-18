"""Phase 1b — the approval primitive.

Covers four things the platform could not previously do:

  1. Gate a *destructive action* of a tool that is not itself a "write tool"
     (``cronjob(action="delete")``) — the gap that let deletions of scheduled
     jobs, kanban cards and .env keys run with no confirmation.
  2. Never gate agent-internal scratch state (``memory`` / ``todo``), the
     over-blocking failure mode that froze every turn in ``awaiting_approval``.
  3. Scope an approval to the exact call it was granted for, so approving one
     tool call cannot widen anything else — and so a nested call made from
     inside an approved call still has to ask.
  4. Fail CLOSED when the agent's authorization posture cannot be read, instead
     of silently degrading to "all tools allowed".

Plus standing grants ("allow for this conversation") so one approval stops the
same class of action from re-prompting every turn.
"""

import logging

import pytest

from app.services.permissions import (
    APPROVAL_GRANTS_KEY,
    PermissionChecker,
    PermissionConfig,
    check_permission,
    grant_for,
    has_standing_grant,
    make_approval_context,
    resolve_risk,
)
from app.services.permissions.approval import arguments_digest, parse_grants


# ---------------------------------------------------------------------------
# 1. Destructive actions are gated even when the tool is not a "write tool"
# ---------------------------------------------------------------------------

def test_destructive_action_is_gated_on_a_non_write_tool():
    """cronjob is not in WRITE_TOOLS, but cronjob(action="delete") destroys a job."""
    from app.services.permissions import WRITE_TOOLS
    assert "cronjob" not in WRITE_TOOLS, "precondition: cronjob is not a legacy write tool"

    result = check_permission("cronjob", {"action": "delete"})
    assert result.requires_confirmation is True
    assert result.risk == "destructive"
    assert result.action == "delete"


def test_non_destructive_action_of_the_same_tool_is_not_gated():
    """Listing scheduled jobs must never prompt."""
    result = check_permission("cronjob", {"action": "list"})
    assert result.requires_confirmation is False
    assert result.allowed is True


def test_kanban_delete_is_gated():
    result = check_permission("kanban", {"action": "delete", "card_id": "c1"})
    assert result.requires_confirmation is True
    assert result.action == "delete"


def test_env_config_delete_is_gated():
    result = check_permission("update_env_config", {"action": "delete", "key": "FOO"})
    assert result.requires_confirmation is True
    assert result.risk == "destructive"


# ---------------------------------------------------------------------------
# 2. Agent scratch state is never gated (the over-blocking failure mode)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool", ["memory", "todo", "checkpoint_manager"])
@pytest.mark.parametrize("action", ["remove", "delete", "write", "list"])
def test_scratch_state_tools_are_never_gated(tool, action):
    """Gating these froze the chat in awaiting_approval with nothing to approve."""
    result = check_permission(tool, {"action": action})
    assert result.requires_confirmation is False
    assert result.allowed is True


def test_never_gate_tools_ignore_standing_grant_parsing():
    """A grant can never be the reason a scratch-state tool runs — and is moot."""
    assert resolve_risk("memory", {"action": "remove"}).destructive is False
    assert has_standing_grant("memory", {"action": "remove"}, {}) is False


# ---------------------------------------------------------------------------
# 3. Approval is bound to the exact call
# ---------------------------------------------------------------------------

def test_approval_covers_the_exact_call():
    ctx = make_approval_context("cronjob", {"action": "delete", "id": "j1"})
    args = {"action": "delete", "id": "j1"}
    assert ctx.covers("cronjob", args) is True

    result = check_permission("cronjob", args, approval_context=ctx)
    assert result.requires_confirmation is False
    assert result.allowed is True


def test_approval_does_not_cover_different_arguments():
    """A nested call with different args must still ask."""
    ctx = make_approval_context("cronjob", {"action": "delete", "id": "j1"})
    different = {"action": "delete", "id": "j2"}

    assert ctx.covers("cronjob", different) is False
    result = check_permission("cronjob", different, approval_context=ctx)
    assert result.requires_confirmation is True


def test_approval_does_not_cover_a_different_tool():
    ctx = make_approval_context("cronjob", {"action": "delete", "id": "j1"})
    assert ctx.covers("kanban", {"action": "delete", "id": "j1"}) is False
    result = check_permission("kanban", {"action": "delete", "id": "j1"}, approval_context=ctx)
    assert result.requires_confirmation is True


def test_approval_context_never_overrides_an_authorization_deny():
    """Approval is consent, not authorization."""
    ctx = make_approval_context("cronjob", {"action": "delete"})
    checker = PermissionChecker(PermissionConfig(denied_tools=["cronjob"]))
    result = checker.check_tool("cronjob", {"action": "delete"}, approval_context=ctx)
    assert result.allowed is False


def test_approval_context_never_overrides_plan_mode():
    ctx = make_approval_context("write_file", {"path": "/tmp/x"})
    checker = PermissionChecker(PermissionConfig(mode="plan"))
    result = checker.check_tool("write_file", {"path": "/tmp/x"}, approval_context=ctx)
    assert result.allowed is False
    assert "plan mode" in result.reason.lower()


def test_inactive_approval_context_is_harmless():
    from app.services.permissions.approval import ApprovalContext
    empty = ApprovalContext()
    assert empty.active is False
    assert empty.covers("cronjob", {"action": "delete"}) is False

    result = check_permission("cronjob", {"action": "delete"}, approval_context=empty)
    assert result.requires_confirmation is True


def test_argument_digest_ignores_key_order_and_private_keys():
    assert arguments_digest({"a": 1, "b": 2}) == arguments_digest({"b": 2, "a": 1})
    assert arguments_digest({"a": 1}) == arguments_digest({"a": 1, "_tool_call_id": "x"})
    assert arguments_digest({"a": 1}) != arguments_digest({"a": 2})
    assert arguments_digest(None) == arguments_digest({})


# ---------------------------------------------------------------------------
# 4. Standing grants — one approval covers the class of action in a conversation
# ---------------------------------------------------------------------------

def test_standing_grant_suppresses_repeat_prompting():
    grant = grant_for("cronjob", {"action": "delete", "id": "j1"})
    meta = {APPROVAL_GRANTS_KEY: [grant]}

    # Same tool + action, different target object — still covered.
    result = check_permission("cronjob", {"action": "delete", "id": "j9"},
                              conversation_metadata=meta)
    assert result.requires_confirmation is False
    assert result.allowed is True


def test_standing_grant_does_not_leak_across_tools():
    grant = grant_for("cronjob", {"action": "delete"})
    meta = {APPROVAL_GRANTS_KEY: [grant]}
    result = check_permission("kanban", {"action": "delete"}, conversation_metadata=meta)
    assert result.requires_confirmation is True


def test_standing_grant_is_action_scoped():
    """A grant for delete must not authorise a different action of the same tool."""
    grant = grant_for("kanban", {"action": "delete"})
    meta = {APPROVAL_GRANTS_KEY: [grant]}
    assert has_standing_grant("kanban", {"action": "delete"}, meta) is True
    assert has_standing_grant("kanban", {"action": "move"}, meta) is False


def test_standing_grant_helper_never_raises_on_garbage():
    for garbage in (None, {}, {APPROVAL_GRANTS_KEY: "not-a-list"},
                    {APPROVAL_GRANTS_KEY: [None, 42, {"nope": 1}]}):
        assert parse_grants(garbage) == []          # type: ignore[arg-type]
        assert has_standing_grant("cronjob", {"action": "delete"}, garbage) is False  # type: ignore[arg-type]


def test_standing_grant_unknown_scope_defaults_to_conversation():
    meta = {APPROVAL_GRANTS_KEY: [{
        "tool_name": "cronjob", "action": "delete", "scope": "galaxy",
    }]}
    grants = parse_grants(meta)
    assert len(grants) == 1 and grants[0].scope == "conversation"


# ---------------------------------------------------------------------------
# 5. Fail CLOSED when authorization cannot be resolved
# ---------------------------------------------------------------------------

def test_unreadable_agent_definition_fails_closed():
    """allowed_tools is an authorization posture: an error must not widen it."""
    from unittest.mock import patch

    with patch(
        "app.services.agent_definitions.get_agent_definition",
        side_effect=RuntimeError("registry exploded"),
    ):
        result = check_permission("write_file", {"path": "/tmp/x"}, agent_name="some_agent")

    assert result.allowed is False
    assert "authorization" in result.reason.lower()


def test_readable_agent_definition_still_allows():
    """The fail-closed path must not fire on the normal path."""
    result = check_permission("web_search", {"query": "x"}, agent_name="general_assistant")
    assert result.allowed is True


def test_agent_name_none_is_not_a_failure():
    result = check_permission("web_search", {"query": "x"})
    assert result.allowed is True


def test_fail_closed_path_logs_the_error(caplog):
    from unittest.mock import patch

    with caplog.at_level(logging.ERROR):
        with patch(
            "app.services.agent_definitions.get_agent_definition",
            side_effect=RuntimeError("registry exploded"),
        ):
            check_permission("write_file", {"path": "/tmp/x"}, agent_name="some_agent")
    assert any("fail" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# 6. The tool declares its own risk (registry metadata beats the fallback table)
# ---------------------------------------------------------------------------

def test_registry_declaration_is_used_and_beats_fallback():
    from app.services.tool_registry import ToolRegistry

    registry = ToolRegistry.get_instance()
    name = "_test_risk_declaration_tool"
    try:
        registry.register(
            name=name,
            schema={"type": "function", "function": {"name": name, "parameters": {}}},
            handler=lambda *a, **k: {"success": True},
            risk="destructive",
            dangerous_actions=("purge",),
        )
        entry = registry.get_entry(name)
        assert entry.risk == "destructive"
        assert "purge" in entry.dangerous_actions

        # The declared action is gated; a sibling action is not.
        assert resolve_risk(name, {"action": "purge"}).destructive is True
        assert check_permission(name, {"action": "purge"}).requires_confirmation is True
        assert resolve_risk(name, {"action": "list"}).destructive is False
    finally:
        registry.deregister(name)


def test_registry_rejects_an_invalid_risk_level():
    from app.services.tool_registry import ToolRegistry

    registry = ToolRegistry.get_instance()
    name = "_test_invalid_risk_tool"
    try:
        registry.register(
            name=name,
            schema={"type": "function", "function": {"name": name, "parameters": {}}},
            handler=lambda *a, **k: {"success": True},
            risk="whatever",
        )
        assert registry.get_entry(name).risk == ""
    finally:
        registry.deregister(name)


def test_unknown_tool_defaults_to_read_without_gating():
    result = check_permission("_no_such_tool_anywhere", {"x": 1})
    assert result.allowed is True
    assert result.requires_confirmation is False


# ---------------------------------------------------------------------------
# 7. Nothing regressed for the legacy list
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool", ["write_file", "create_agent", "execute_code"])
def test_legacy_write_tools_still_require_confirmation(tool):
    result = check_permission(tool, {})
    assert result.requires_confirmation is True
    assert result.allowed is True


def test_legacy_read_tools_still_silent():
    for tool in ("read_file", "web_search", "list_tools"):
        assert check_permission(tool, {}).requires_confirmation is False


def test_plan_mode_still_blocks_writes():
    checker = PermissionChecker(PermissionConfig(mode="plan"))
    assert checker.check_tool("write_file", {"path": "/tmp/x"}).allowed is False


def test_sensitive_paths_still_blocked_under_full_auto():
    """The fix for the escalation bug must not weaken path protection."""
    checker = PermissionChecker(PermissionConfig(mode="full_auto"))
    result = checker.check_tool("write_file", {"path": "/Users/someone/.ssh/id_rsa"})
    assert result.allowed is False
