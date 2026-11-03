"""Spec-compliant MCP server transport — Zhanlu skills over streamable HTTP.

Builds a ``FastMCP`` server (official SDK) that exposes every registered
ToolRegistry handler as an MCP tool, mounted at ``/api/ext-mcp``. This
gives external MCP clients (Claude Desktop, Cursor, n8n, …)
protocol-version negotiation, capabilities, sessions and both JSON and
SSE responses for free — the 2026-spec server surface. External clients
point their base URL at ``https://<host>/api/ext-mcp/mcp``.
"""
import json
import logging
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.services.mcp_server import get_zhanlu_mcp_server
from app.services.mcp_server.mcp_auth import (  # noqa: F401 — re-exports
    extract_zhanlu_context, verify_mcp_context)
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

_fastmcp: FastMCP | None = None

def _audit_tool_call(db, principal: dict, tool_name: str,
                     agent_scoped: bool) -> None:
    """Phase 8.2 Stage 5: audit a successful privileged tool invocation.
    Org from the VERIFIED principal; ids only — never arguments/results
    (no sensitive payloads), never credential material."""
    try:
        import uuid as _uuid
        from datetime import datetime as _dt

        from app.models.audit_event import AuditEvent

        db.add(AuditEvent(
            id=str(_uuid.uuid4()),
            event_type="mcp_tool_invoked",
            event_source="mcp",
            actor_type="user",
            actor_id=principal.get("user_id"),
            org_id=principal.get("org_id"),
            resource_type="mcp_tool",
            resource_id=tool_name,
            outcome="success",
            detail_json={"agent_scoped": agent_scoped},
            occurred_at=_dt.utcnow(),
        ))
        db.commit()
    except Exception as exc:  # noqa: BLE001 — audit is fail-open
        logger.warning("mcp: tool audit write failed (non-fatal): %s", exc)


class McpAuthMiddleware:
    """Pure-ASGI auth for the external MCP transport.

    * Public protocol negotiation (initialize / tools/list / notifications /
      ping / GET SSE stream) passes through unauthenticated — it exposes
      static tool metadata only.
    * Privileged execution (tools/call) requires a valid Bearer JWT
      (existing auth machinery: signature + expiry + JTI revocation + DB
      User row). Failures short-circuit with a 401 static JSON-RPC error —
      never anonymous privileged access.
    * The authenticated principal is stored in a contextvar for the tool
      layer; it is the ONLY source of identity — _zhanlu_context claims
      can never override it (see mcp_auth.resolve_execution_context).
    * Auth success/failure is audited (org attribution only when a valid
      token resolved the user; NEVER any token/header material).
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await self.inner(scope, receive, send)

        # Buffer the request body so we can (a) inspect the JSON-RPC method
        # and (b) replay it to the inner app.
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"") or b""
            if not message.get("more_body", False):
                break

        from app.services.mcp_server.mcp_auth import (
            authenticate_mcp_request,
            is_privileged_jsonrpc,
            set_principal,
        )

        if not is_privileged_jsonrpc(body):
            replay = await self._replaying_receive(body)
            return await self.inner(scope, replay, send)

        # Privileged: authenticate BEFORE any execution.
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            header = self._header(scope, "authorization")
            auth = authenticate_mcp_request(db, header)
            request_id = self._request_id(body)
            if auth is None:
                self._audit(db, None, "mcp_auth_failed",
                            "missing, malformed, invalid, expired or "
                            "revoked credentials")
                db.commit()
                await self._send_401(send, request_id)
                return
            user, claims = auth
            set_principal({"user_id": user.id, "org_id": user.org_id,
                           "role": user.role, "claims": claims})
            self._audit(db, user, "mcp_auth_success", None)
            db.commit()
        finally:
            db.close()

        replay = await self._replaying_receive(body)
        return await self.inner(scope, replay, send)

    # -- helpers ----------------------------------------------------------

    async def _replaying_receive(self, body: bytes):
        """Receive() that replays the buffered body, then signals EOF."""
        async def receive():
            return {"type": "http.request", "body": body,
                    "more_body": False}
        return receive

    @staticmethod
    def _header(scope, name: str) -> str | None:
        target = name.encode()
        for key, value in scope.get("headers") or []:
            if key.lower() == target:
                return value.decode("latin-1")
        return None

    @staticmethod
    def _request_id(body: bytes):
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                return parsed.get("id")
        except Exception:
            pass
        return None

    @staticmethod
    async def _send_401(send, request_id):
        payload = json.dumps({
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32001,
                      "message": "authentication required"},
        }).encode()
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": payload})

    @staticmethod
    def _audit(db, user, event_type: str, reason: str | None) -> None:
        """Security audit with org attribution when a principal resolved.
        NEVER records tokens, headers, or credential material."""
        try:
            import uuid as _uuid
            from datetime import datetime as _dt

            from app.models.audit_event import AuditEvent

            kwargs = {
                "id": str(_uuid.uuid4()),
                "event_type": event_type,
                "event_source": "mcp",
                "actor_type": "user",
                "actor_id": getattr(user, "id", None),
                "resource_type": "mcp_transport",
                "outcome": "success" if reason is None else "denied",
                "detail_json": (
                    {"reason": reason} if reason else None),
                "occurred_at": _dt.utcnow(),
            }
            if user is not None:
                kwargs["org_id"] = getattr(user, "org_id", None)
            db.add(AuditEvent(**kwargs))
        except Exception as exc:  # noqa: BLE001 — audit is fail-open here
            logger.warning("mcp: audit write failed (non-fatal): %s", exc)


# Phase 8 T8-7: the ONLY context keys an MCP caller may propagate. Identity
# and trace references that the tool handlers' org/binding scoping already
# re-validates server-side — never permission flags or trust overrides.
_ALLOWED_CONTEXT_KEYS = frozenset({
    "org_id", "app_id", "user_id", "agent_app_id", "agent_name",
    "run_id", "conversation_id",
})
_RESERVED_CONTEXT_ARG = "_zhanlu_context"


def extract_zhanlu_context(arguments: dict | None) -> tuple[dict | None, dict]:
    """Split ``_zhanlu_context`` out of MCP tool arguments.

    Returns ``(context_or_None, remaining_arguments)``. A missing or
    non-dict context yields ``(None, arguments_without_key)`` — anonymous
    MCP calls keep the no-context fallback; garbage context is DROPPED,
    never half-trusted. Unknown keys inside a dict context are stripped
    (allow-list, so permission flags cannot ride along)."""
    if not isinstance(arguments, dict):
        return None, {}
    rest = {k: v for k, v in arguments.items()
            if k != _RESERVED_CONTEXT_ARG}
    raw = arguments.get(_RESERVED_CONTEXT_ARG)
    if not isinstance(raw, dict):
        return None, rest
    return {k: v for k, v in raw.items() if k in _ALLOWED_CONTEXT_KEYS}, rest


def verify_mcp_context(db, context: dict | None) -> dict | None:
    """Server-side verification of caller-supplied MCP context claims.

    ``_zhanlu_context`` is a CLAIM, not an identity. Before any claim may
    influence authorization it is validated against the database:
      * ``org_id`` must reference a real Organization;
      * ``agent_app_id`` (when claimed) must be a live AgentApp of THAT org;
      * ``user_id`` (when claimed) must be a User of THAT org.
    Any unverifiable claim drops the WHOLE context to None (fail-honest
    no-context fallback) — never a partially-trusted one. A context with
    no identity claims at all passes through UNVERIFIED (marked
    ``verified: False``) — tools treat it exactly like no context.
    A fully verified context is stamped ``verified: True``.
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
        except Exception as exc:  # noqa: BLE001 — verification must fail closed
            logger.warning("mcp: context verification failed: %s", exc)
            return None
    # no org claim — nothing to verify; pass through UNVERIFIED
    return {**context, "verified": False}


def build_fastmcp_server() -> FastMCP:
    """Create a FastMCP server wrapping the ToolRegistry (idempotent)."""
    global _fastmcp
    if _fastmcp is not None:
        return _fastmcp

    server = FastMCP(
        "zhanlu-skills",
        instructions=(
            "Zhanlu Enterprise AI OS — governed skills exposed as MCP tools. "
            "Each tool takes a single JSON `arguments` object."
        ),
        # Served behind the main Zhanlu ASGI app on the deployment's real
        # Host header; disable FastMCP's localhost-only DNS-rebinding guard
        # (it would 421 every request with a non-localhost Host).
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
    zhanlu = get_zhanlu_mcp_server()

    def _make_tool(tool_name: str, entry) -> Callable:
        async def _tool(arguments: dict | None = None) -> str:
            # Phase 8.2 Stages 3+4: the authorization boundary lives in
            # mcp_auth.resolve_execution_context (unit-tested): the
            # AUTHENTICATED principal is the sole source of identity;
            # claims must match it; datasource scope comes ONLY from the
            # verified agent's enabled bindings. Anonymous callers keep
            # the fail-honest no-context fallback.
            from app.database import SessionLocal as _SessionLocal
            from app.services.mcp_server.mcp_auth import (
                get_principal,
                resolve_execution_context,
            )

            principal = get_principal()
            _vdb = _SessionLocal()
            try:
                context, tool_arguments = resolve_execution_context(
                    _vdb, arguments, principal)
                content = await zhanlu.call_tool(
                    tool_name, tool_arguments, context=context)
                if principal is not None:
                    _audit_tool_call(_vdb, principal, tool_name,
                                     bool((context or {}).get("agent_app_id")))
            finally:
                _vdb.close()
            texts = [
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            ]
            return "\n".join(t for t in texts if t) or json.dumps(
                content, ensure_ascii=False, default=str,
            )

        _tool.__name__ = tool_name
        _tool.__doc__ = entry.description or tool_name
        return _tool

    for name in sorted(registry.list_available()):
        entry = registry.get_entry(name)
        if not entry or not entry.handler:
            continue
        server.tool()(_make_tool(name, entry))

    _fastmcp = server
    logger.info("FastMCP server built with %d tools", len(registry.list_available()))
    return server


def get_mcp_http_app() -> Any:
    """ASGI app for streamable HTTP transport (mount at /api/ext-mcp).

    Phase 8.2: wrapped in the authentication middleware — privileged
    JSON-RPC methods (tools/call) require valid credentials; public
    negotiation passes through. All transports inherit the protection
    because every transport path goes through this one ASGI app."""
    return McpAuthMiddleware(build_fastmcp_server().streamable_http_app())


def get_fastmcp_lifespan() -> Any:
    """Async context manager driving the FastMCP session-manager lifecycle.

    Starlette ``Mount`` does not run a child app's lifespan, so the host app
    must enter ``session_manager.run()`` itself (uvicorn does this for the
    standalone app; this hook is for the mounted-in-main case).
    """
    return build_fastmcp_server().session_manager.run()


__all__ = ["build_fastmcp_server", "get_mcp_http_app", "get_fastmcp_lifespan"]
