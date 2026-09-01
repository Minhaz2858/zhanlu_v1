"""Tests for the RBAC authorization provider."""
from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.services.authz import get_authorizer
from app.services.authz.base import AuthorizationDecision, ResourceType
from app.services.authz.rbac import BUILTIN_ROLES, RbacAuthorizationProvider
from app.services.tracing import TraceContext
from app.services.tool_registry import ToolRegistry


def test_admin_role_allows_all_tools():
    authz = RbacAuthorizationProvider()
    decision = authz.can_access(role="admin", resource_type=ResourceType.TOOL, resource_id="execute_code")
    assert decision.allowed is True


def test_user_role_allows_all_tools_by_default():
    authz = RbacAuthorizationProvider()
    decision = authz.can_access(role="user", resource_type=ResourceType.TOOL, resource_id="write_file")
    assert decision.allowed is True


def test_viewer_role_denies_write_tools():
    authz = RbacAuthorizationProvider()
    decision = authz.can_access(role="viewer", resource_type=ResourceType.TOOL, resource_id="write_file")
    assert decision.allowed is False


def test_viewer_role_allows_read_tools():
    authz = RbacAuthorizationProvider()
    for tool in ["read_file", "web_search", "list_tools"]:
        decision = authz.can_access(role="viewer", resource_type=ResourceType.TOOL, resource_id=tool)
        assert decision.allowed is True, f"viewer should access {tool}"


def test_unknown_role_is_denied_fail_closed():
    authz = RbacAuthorizationProvider()
    decision = authz.can_access(role="unknown_role", resource_type=ResourceType.TOOL, resource_id="read_file")
    assert decision.allowed is False
    assert "unknown role" in decision.reason.lower()


def test_filter_resources_removes_denied():
    authz = RbacAuthorizationProvider()
    tools = ["read_file", "write_file", "web_search", "execute_code"]
    allowed = authz.filter_resources(role="viewer", resource_type=ResourceType.TOOL, resource_ids=tools)
    assert "read_file" in allowed
    assert "web_search" in allowed
    assert "write_file" not in allowed
    assert "execute_code" not in allowed


def test_filter_resources_admin_keeps_all():
    authz = RbacAuthorizationProvider()
    tools = ["read_file", "write_file", "execute_code"]
    allowed = authz.filter_resources(role="admin", resource_type=ResourceType.TOOL, resource_ids=tools)
    assert set(allowed) == set(tools)


def test_deny_always_wins_over_allow():
    custom_roles = {"test_role": {"tools": {"allow": ["read_file", "write_file"], "deny": ["write_file"]}}}
    authz = RbacAuthorizationProvider(roles=custom_roles)
    decision = authz.can_access(role="test_role", resource_type=ResourceType.TOOL, resource_id="write_file")
    assert decision.allowed is False


def test_policy_compilation_rejects_unknown_keys():
    import pytest
    bad_roles = {"bad_role": {"tools": {"allow": ["read_file"]}, "unknown_section": {"foo": "bar"}}}
    with pytest.raises(ValueError, match="unknown_section"):
        RbacAuthorizationProvider(roles=bad_roles)


def test_builtin_roles_exist():
    assert "admin" in BUILTIN_ROLES
    assert "user" in BUILTIN_ROLES
    assert "viewer" in BUILTIN_ROLES


def test_permissive_authorizer_allows_everything():
    from app.services.authz.rbac import PermissiveAuthorizer
    authz = PermissiveAuthorizer()
    decision = authz.can_access(role="anything", resource_type=ResourceType.TOOL, resource_id="anything")
    assert decision.allowed is True


def test_get_authorizer_returns_rbac_by_default():
    authz = get_authorizer()
    assert isinstance(authz, RbacAuthorizationProvider)


# ---------------------------------------------------------------------------
# tool_registry.get_schemas() RBAC integration tests
#
# These verify the integration point that wires RBAC into the LLM tool list:
# get_schemas() reads TraceContext.current_role() and strips denied tools
# before they reach the model. A fresh (non-singleton) ToolRegistry is used
# so the global registry singleton — relied on by other test modules — is
# never mutated.
# ---------------------------------------------------------------------------

def _dummy_tool_schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"dummy {name}",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _dummy_handler(args, db, user_id, **ctx):
    return {}


def _registry_with_tools(*names: str) -> ToolRegistry:
    """Fresh (non-singleton) registry with dummy tools registered."""
    reg = ToolRegistry()
    for name in names:
        reg.register(name, _dummy_tool_schema(name), _dummy_handler)
    return reg


# Tool names chosen to match the built-in RBAC policy:
#   read_file    -> viewer-allowed
#   write_file   -> viewer-denied
#   execute_code -> viewer-denied
_RBAC_TEST_TOOLS = ["read_file", "write_file", "execute_code"]


def test_get_schemas_filters_denied_tools_for_viewer():
    reg = _registry_with_tools(*_RBAC_TEST_TOOLS)
    TraceContext.clear()
    try:
        TraceContext.set(role="viewer")
        schemas = reg.get_schemas(_RBAC_TEST_TOOLS)
        names = [s["function"]["name"] for s in schemas]
        assert "read_file" in names
        assert "write_file" not in names
        assert "execute_code" not in names
    finally:
        TraceContext.clear()


def test_get_schemas_keeps_all_for_user():
    reg = _registry_with_tools(*_RBAC_TEST_TOOLS)
    TraceContext.clear()
    try:
        TraceContext.set(role="user")
        schemas = reg.get_schemas(_RBAC_TEST_TOOLS)
        names = [s["function"]["name"] for s in schemas]
        assert set(names) == set(_RBAC_TEST_TOOLS)
    finally:
        TraceContext.clear()


def test_get_schemas_no_role_falls_back_to_user():
    """When no role is set on TraceContext, get_schemas defaults to 'user'
    (permissive) — matching the production path before handlers wire the role.
    """
    reg = _registry_with_tools(*_RBAC_TEST_TOOLS)
    TraceContext.clear()
    try:
        schemas = reg.get_schemas(_RBAC_TEST_TOOLS)
        names = [s["function"]["name"] for s in schemas]
        assert set(names) == set(_RBAC_TEST_TOOLS)
    finally:
        TraceContext.clear()
