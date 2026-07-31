"""RBAC authorization provider — deny-always-wins, fail-closed."""
from __future__ import annotations

import logging
from typing import Any

from app.services.authz.base import AuthorizationDecision, AuthorizationProvider, ResourceType

logger = logging.getLogger(__name__)

_VIEWER_ALLOWED_TOOLS: list[str] = [
    "read_file", "web_search", "web_extract",
    "list_tools", "list_knowledge_bases", "list_market_agents",
]

BUILTIN_ROLES: dict[str, dict[str, Any]] = {
    "admin": {"tools": {"allow": None, "deny": []}},
    "user": {"tools": {"allow": None, "deny": []}},
    "viewer": {"tools": {"allow": _VIEWER_ALLOWED_TOOLS, "deny": []}},
}

_VALID_RESOURCE_KEYS = {"tools", "models", "skills", "sandbox", "mcp_servers", "routes"}
_VALID_POLICY_KEYS = {"allow", "deny"}

_SECTION_TO_RESOURCE: dict[str, ResourceType] = {
    "tools": ResourceType.TOOL, "models": ResourceType.MODEL,
    "skills": ResourceType.SKILL, "sandbox": ResourceType.SANDBOX,
    "mcp_servers": ResourceType.MCP_SERVER, "routes": ResourceType.ROUTE,
}


class RbacAuthorizationProvider(AuthorizationProvider):
    """Role-based authorization: deny-always-wins, fail-closed."""

    def __init__(self, roles: dict[str, dict[str, Any]] | None = None):
        self._roles = roles if roles is not None else BUILTIN_ROLES
        self._compiled = self._compile(self._roles)

    def _compile(self, roles):
        compiled = {}
        for role_name, policy in roles.items():
            compiled[role_name] = {}
            for key, section in policy.items():
                if key not in _VALID_RESOURCE_KEYS:
                    raise ValueError(f"Unknown policy section '{key}' in role '{role_name}'.")
                rt = _SECTION_TO_RESOURCE.get(key)
                if rt is None:
                    continue
                if not isinstance(section, dict):
                    raise ValueError(f"Section '{key}' in role '{role_name}' must be a dict")
                for sub_key in section:
                    if sub_key not in _VALID_POLICY_KEYS:
                        raise ValueError(f"Unknown policy key '{sub_key}' in role '{role_name}'.{key}")
                allow = section.get("allow")
                deny = section.get("deny", [])
                allow_set = set(allow) if allow is not None else None
                deny_set = set(deny) if deny else set()
                compiled[role_name][rt] = (allow_set, deny_set)
        return compiled

    def can_access(self, *, role, resource_type, resource_id, action="use"):
        role_policy = self._compiled.get(role)
        if role_policy is None:
            return AuthorizationDecision(allowed=False, reason=f"Access denied: unknown role '{role}' (fail-closed)")
        allow_set, deny_set = role_policy.get(resource_type, (None, set()))
        if resource_id in deny_set:
            return AuthorizationDecision(allowed=False, reason=f"Access denied: '{resource_id}' is denied for role '{role}'", denied_resources=[resource_id])
        if allow_set is not None and resource_id not in allow_set:
            return AuthorizationDecision(allowed=False, reason=f"Access denied: '{resource_id}' not in allowed list for role '{role}'", denied_resources=[resource_id])
        return AuthorizationDecision(allowed=True)

    def filter_resources(self, *, role, resource_type, resource_ids):
        return [rid for rid in resource_ids if self.can_access(role=role, resource_type=resource_type, resource_id=rid).allowed]


class PermissiveAuthorizer(AuthorizationProvider):
    """Authorizer that allows everything — for testing/legacy mode."""

    def can_access(self, *, role, resource_type, resource_id, action="use"):
        return AuthorizationDecision(allowed=True)

    def filter_resources(self, *, role, resource_type, resource_ids):
        return list(resource_ids)
