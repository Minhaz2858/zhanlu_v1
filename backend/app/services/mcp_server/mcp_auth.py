"""Phase 8.2 — external MCP authentication, verification, authorization.

Single home for the external-MCP trust boundary:

* ``authenticate_mcp_request`` — WHO is calling (existing JWT machinery:
  signature, expiry, JTI revocation blacklist, DB User row);
* ``extract_zhanlu_context`` — split supplementary claims out of tool
  arguments (allow-list; permission flags can never ride along);
* ``verify_mcp_context`` — are the CLAIMED org/user/agent references
  real, live, and same-org (existence + ownership checks);
* ``resolve_execution_context`` — the tool-execution authorization
  boundary: the AUTHENTICATED principal is authoritative, claims must
  match it, and datasource scope comes ONLY from the verified agent's
  enabled AgentDataBinding rows (no client-supplied scope, no
  query-all, no default/first datasource).

Fail-closed everywhere: unverifiable identity or context yields None or
an empty scope — never anonymous privileged access, never partial trust.
"""

from __future__ import annotations

import contextvars
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Authenticated principal for the CURRENT request (set by the ASGI auth
# middleware before the app runs; read by the tool layer). A contextvar —
# never a global — so concurrent requests cannot see each other's identity.
principal_var: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar("mcp_principal", default=None))

# The ONLY context keys an MCP caller may propagate. Identity and trace
# references that the tool handlers' org/binding scoping re-validates
# server-side — never permission flags or trust overrides.
_ALLOWED_CONTEXT_KEYS = frozenset({
    "org_id", "app_id", "user_id", "agent_app_id", "agent_name",
    "run_id", "conversation_id",
})
_RESERVED_CONTEXT_ARG = "_zhanlu_context"

_PRIVILEGED_METHODS = {"tools/call"}


# ── authentication ──────────────────────────────────────────────────────────

def authenticate_mcp_request(db, authorization: str | None):
    """Resolve the caller identity from an ``Authorization`` header value.

    Returns ``(user, claims)`` on success, None on ANY failure
    (missing/malformed/invalid/expired/revoked token, deleted user).
    """
    if not authorization or not isinstance(authorization, str):
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        return None
    token = parts[1].strip()
    try:
        from app.services.auth_service import AuthService

        claims = AuthService().verify_token_claims(token, db)
    except Exception:  # noqa: BLE001 — auth must fail closed
        return None
    if not claims:
        return None
    try:
        from app.models.user import User

        user = (
            db.query(User)
            .filter(User.id == claims.get("sub"),
                    User.is_deleted == False)  # noqa: E712
            .first()
        )
    except Exception:  # noqa: BLE001
        return None
    if user is None:
        return None
    return user, claims


def get_principal() -> dict[str, Any] | None:
    """The authenticated principal for the current request (or None)."""
    return principal_var.get()


def set_principal(principal: dict[str, Any] | None) -> None:
    principal_var.set(principal)


def is_privileged_jsonrpc(body: bytes) -> bool:
    """True when the JSON-RPC body performs a privileged operation.

    Only ``tools/call`` executes tools against Zhanlu data, so only it
    requires authentication. Protocol-level methods (``initialize``,
    ``tools/list``, ``ping``, notifications) pass through unauthenticated
    — public protocol negotiation is separated from privileged
    execution. Batched requests are privileged if ANY member is.
    Unparseable bodies can never contain ``tools/call`` — the MCP
    protocol layer itself rejects them — so they pass through public.
    """
    try:
        parsed = json.loads(body) if body else None
    except (ValueError, UnicodeDecodeError):
        return False
    if isinstance(parsed, list):
        return any(
            isinstance(member, dict) and member.get("method") == "tools/call"
            for member in parsed
        )
    if isinstance(parsed, dict):
        return parsed.get("method") == "tools/call"
    return False


# ── supplementary claims ────────────────────────────────────────────────────

def extract_zhanlu_context(arguments: dict | None) -> tuple[dict | None, dict]:
    """Split ``_zhanlu_context`` out of MCP tool arguments.

    Returns ``(context_or_None, remaining_arguments)``. A missing or
    non-dict context yields ``(None, arguments_without_key)``; unknown
    keys inside a dict context are stripped (allow-list).
    """
    if not isinstance(arguments, dict):
        return None, {}
    rest = {k: v for k, v in arguments.items()
            if k != _RESERVED_CONTEXT_ARG}
    raw = arguments.get(_RESERVED_CONTEXT_ARG)
    if not isinstance(raw, dict):
        return None, rest
    return {k: v for k, v in raw.items() if k in _ALLOWED_CONTEXT_KEYS}, rest


def verify_mcp_context(db, context: dict | None) -> dict | None:
    """Server-side verification of caller-supplied context claims.

    ``_zhanlu_context`` is a CLAIM, not an identity. Every identity claim
    is validated against the database: ``org_id`` must reference a real
    Organization; ``agent_app_id`` (when claimed) must be a live AgentApp
    of THAT org; ``user_id`` (when claimed) must be a User of THAT org.
    Any unverifiable claim drops the WHOLE context to None. A context
    with no identity claims passes through UNVERIFIED (``verified:
    False``); a fully verified one is stamped ``verified: True``.
    """
    if not isinstance(context, dict) or not context:
        return None
    org_id = context.get("org_id")
    if org_id:
        try:
            from app.models.agent_app import AgentApp
            from app.models.organization import Organization
            from app.models.user import User

            if not db.query(Organization).filter(
                    Organization.id == org_id).first():
                logger.warning(
                    "mcp: context org claim unverifiable: %s", org_id)
                return None
            agent_app_id = context.get("agent_app_id")
            if agent_app_id:
                agent = db.query(AgentApp).filter(
                    AgentApp.id == agent_app_id,
                    AgentApp.is_deleted == False,  # noqa: E712
                ).first()
                if agent is None or getattr(agent, "org_id", None) != org_id:
                    logger.warning(
                        "mcp: context agent claim unverifiable: %s (org %s)",
                        agent_app_id, org_id)
                    return None
            user_id = context.get("user_id")
            if user_id:
                user = db.query(User).filter(
                    User.id == user_id,
                    User.is_deleted == False,  # noqa: E712
                ).first()
                if user is None or getattr(user, "org_id", None) != org_id:
                    logger.warning(
                        "mcp: context user claim unverifiable: %s (org %s)",
                        user_id, org_id)
                    return None
            return {**context, "verified": True}
        except Exception as exc:  # noqa: BLE001 — fail closed
            logger.warning("mcp: context verification failed: %s", exc)
            return None
    return {**context, "verified": False}


# ── Stages 3+4: the tool-execution authorization boundary ───────────────────

def resolve_execution_context(db, arguments, principal):
    """Merge claims + authenticated principal into the tool execution
    context, enforcing the Stage 3/4 contract:

    * the AUTHENTICATED principal (org/user from the verified DB User row)
      is authoritative — client claims can never override it;
    * claimed ``org_id``/``user_id`` must MATCH the principal — any
      mismatch drops the WHOLE claim context (never partially trusted);
    * claimed ``agent_app_id`` must be a live same-org AgentApp (verified
      via ``verify_mcp_context``);
    * datasource scope comes ONLY from the verified agent's ENABLED
      AgentDataBinding rows — never from the client, never query-all,
      never a default/first datasource.

    Returns ``(context, tool_arguments)``; ``context`` is None for
    anonymous callers (fail-honest no-context fallback).
    """
    context, tool_arguments = extract_zhanlu_context(arguments)
    if principal is None:
        if context is not None:
            # anonymous callers: client claims carry NO identity
            # authority — drop them entirely (fail closed)
            logger.warning(
                "mcp: anonymous caller supplied context claims — dropped")
        return None, tool_arguments

    claims = context or {}
    if (claims.get("org_id") not in (None, principal["org_id"])
            or claims.get("user_id") not in (None, principal["user_id"])):
        logger.warning(
            "mcp: context claims mismatch the authenticated principal — "
            "context dropped")
        context = None
    if context is not None:
        # anchor identity claims to the principal before verification:
        # when the caller omitted org_id, verification of agent/user
        # claims runs against the PRINCIPAL's org — a cross-org agent
        # never enters the execution context
        if context.get("org_id") is None:
            context["org_id"] = principal["org_id"]
        context = verify_mcp_context(db, context)

    # authoritative identity always wins over claims
    context = {**(context or {}),
               "org_id": principal["org_id"],
               "user_id": principal["user_id"],
               "verified": True}

    agent_app_id = context.get("agent_app_id")
    if agent_app_id:
        try:
            from app.services.agent_binding_service import (
                get_bound_data_source_ids_via_bindings,
            )

            bound = get_bound_data_source_ids_via_bindings(db, agent_app_id)
            context["bound_kb_ids"] = list(bound or [])
        except Exception as exc:  # noqa: BLE001 — scope to NONE, never widen
            logger.warning(
                "mcp: binding resolution failed (scoping to none): %s", exc)
            context["bound_kb_ids"] = []
    return context, tool_arguments
