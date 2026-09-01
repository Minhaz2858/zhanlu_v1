"""Authorization package — pluggable RBAC for role-based resource access."""
from __future__ import annotations

from app.services.authz.base import AuthorizationDecision, AuthorizationProvider, ResourceType

_authorizer: AuthorizationProvider | None = None


def get_authorizer() -> AuthorizationProvider:
    """Get the singleton AuthorizationProvider instance."""
    global _authorizer
    if _authorizer is not None:
        return _authorizer
    from app.config import settings
    provider = getattr(settings, "AUTHZ_PROVIDER", "rbac")
    if provider == "none":
        from app.services.authz.rbac import PermissiveAuthorizer
        _authorizer = PermissiveAuthorizer()
    else:
        from app.services.authz.rbac import RbacAuthorizationProvider
        _authorizer = RbacAuthorizationProvider()
    return _authorizer


def reset_authorizer() -> None:
    """Reset the singleton (for testing)."""
    global _authorizer
    _authorizer = None


__all__ = ["AuthorizationProvider", "AuthorizationDecision", "ResourceType", "get_authorizer", "reset_authorizer"]
