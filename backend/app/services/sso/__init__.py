"""SSO / OIDC — provider config, discovery, ID-token verification,
authorization state, and account linking (roadmap item 7)."""

from app.services.sso.account import find_or_create_sso_user, is_email_allowed
from app.services.sso.oidc import (
    DEFAULT_SCOPES,
    SsoProvider,
    fetch_discovery,
    fetch_jwks,
    get_provider,
    get_providers,
    verify_id_token,
)
from app.services.sso.state import SsoStateStore, sso_state_store

__all__ = [
    "DEFAULT_SCOPES",
    "SsoProvider",
    "SsoStateStore",
    "fetch_discovery",
    "fetch_jwks",
    "find_or_create_sso_user",
    "get_provider",
    "get_providers",
    "is_email_allowed",
    "sso_state_store",
    "verify_id_token",
]
