"""MCP (Model Context Protocol) router — manage external MCP servers.

Endpoints:
  POST   /api/mcp/servers/connect     — connect to an external MCP server
  GET    /api/mcp/servers              — list connected MCP servers
  DELETE /api/mcp/servers/{id}         — disconnect a server
  GET    /api/mcp/tools                — list tools available from connected servers

Also exposes the native Zhanlu MCP server tools + resources for
external clients (Claude Desktop, Cursor, etc.) to discover.
"""

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_required

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp"], dependencies=[Depends(get_current_user_required)])


# ─── Request / Response Models ──────────────────────────────────────────

class McpConnectRequest(BaseModel):
    """Connect to an external MCP server (stdio or streamable HTTP).

    stdio: ``command`` + ``args`` + ``env`` (Claude-Desktop-style config).
    http: ``base_url`` (e.g. ``https://example.com/mcp``) + optional
    ``auth_type`` (none | bearer | oauth).
    """
    name: str
    command: Optional[str] = None
    args: Optional[list[str]] = None
    env: Optional[dict[str, str]] = None
    description: Optional[str] = None
    transport: str = "stdio"  # stdio | http | streamable (legacy)
    base_url: Optional[str] = None
    auth_type: str = "none"
    marketplace_id: Optional[str] = None


class McpToolCallRequest(BaseModel):
    arguments: Optional[dict] = None


class OAuthStartRequest(BaseModel):
    client_id: str
    client_secret: Optional[str] = None
    scopes: Optional[str] = None


class OAuthCallbackRequest(BaseModel):
    code: str
    state: str


class McpServerResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    transport: str
    status: str
    tools_count: int
    resources_count: int


class McpGrantsRequest(BaseModel):
    """Which agents may use this server's tools.

    An empty list grants the server to every agent (the default). Names are
    ``AgentConversation.agent_name`` values.
    """
    granted_agents: list[str] = []


# ─── Authorization ──────────────────────────────────────────────────────

def _require_mcp_access(resource_id: str, user=None, action: str = "use") -> None:
    """Deny MCP server use when the caller's role holds no grant (fail-closed).

    2026-09-17: none of these endpoints checked authorization at all. A stdio
    MCP server's ``server_url`` is an arbitrary shell command, so the connect
    endpoint was an unauthenticated role-escalation path: any logged-in role
    could persist a command that the backend later spawns as its own user.

    2026-09-17 (second pass): the first version took the role from
    ``TraceContext.current_role()`` — but NOTHING sets that outside the agents
    router's chat endpoints (there is no middleware at all), so every /mcp
    endpoint fell through to the "user" default and the gate denied nothing.
    Measured, not assumed: a real ``viewer`` account got HTTP 200 on connect.
    The role now comes from the authenticated user object, with TraceContext
    kept only as a fallback for callers that do populate it.
    """
    from app.services.authz import get_authorizer
    from app.services.authz.base import ResourceType
    from app.services.tracing import TraceContext

    role = getattr(user, "role", None) or TraceContext.current_role() or "user"
    decision = get_authorizer().can_access(
        role=role,
        resource_type=ResourceType.MCP_SERVER,
        resource_id=resource_id,
        action=action,
    )
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=decision.reason)


# ─── Native MCP endpoint (for external clients) ─────────────────────────

@router.get("/mcp/tools")
def list_native_mcp_tools():
    """List all Zhanlu tools as MCP-compatible tools (for external clients)."""
    from app.services.mcp_server import get_zhanlu_mcp_server
    server = get_zhanlu_mcp_server()
    return {"tools": server.list_tools()}


@router.get("/mcp/resources")
def list_native_mcp_resources():
    """List all Zhanlu skills as MCP resources."""
    from app.services.mcp_server import get_zhanlu_mcp_server
    server = get_zhanlu_mcp_server()
    return {"resources": server.list_resources()}


@router.post("/mcp/call")
async def call_native_mcp_tool(
    name: str = Query(...),
    args: Optional[str] = Query(None),
):
    """Call a Zhanlu tool via MCP protocol. Args as JSON string."""
    from app.services.mcp_server import get_zhanlu_mcp_server
    server = get_zhanlu_mcp_server()

    arguments = {}
    if args:
        try:
            arguments = json.loads(args)
        except json.JSONDecodeError:
            pass

    content = await server.call_tool(name, arguments)
    return {"content": content}


# ─── External MCP server management ─────────────────────────────────────

@router.post("/mcp/servers/connect")
def connect_mcp_server(
    req: McpConnectRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
):
    """Connect to an external MCP server (stdio or streamable HTTP).

    stdio config shape (same as Claude Desktop):
    ```
    {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
     "env": {"GITHUB_TOKEN": "..."}}
    ```
    HTTP config shape (2026 spec):
    ```
    {"transport": "http", "base_url": "https://example.com/mcp",
     "auth_type": "oauth" | "bearer" | "none"}
    ```
    The server is probed in the background (initialize + tools/list) and the
    row's ``capabilities``/``tools_count`` are updated when it completes.
    """
    from app.models.mcp_server import McpServer

    _require_mcp_access(req.name, user, action="create")

    transport = (req.transport or "stdio").lower()
    if transport == "http":
        if not req.base_url:
            raise HTTPException(status_code=400, detail="base_url is required for http transport")
        server_url = req.base_url.rstrip("/")
    else:
        if not req.command:
            raise HTTPException(status_code=400, detail="command is required for stdio transport")
        server_url = f"{req.command} {' '.join(req.args or [])}".strip()

    existing = (
        db.query(McpServer)
        .filter(McpServer.name == req.name, McpServer.is_deleted == False)
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail=f"MCP server '{req.name}' already connected")

    mcp = McpServer(
        name=req.name,
        description=req.description,
        server_url=server_url,
        base_url=req.base_url.rstrip("/") if req.base_url else None,
        transport=transport,
        auth_type=req.auth_type or "none",
        status="connecting",
        tools_count=0,
        resources_count=0,
    )
    db.add(mcp)
    db.commit()
    db.refresh(mcp)

    # Probe in the background with a fresh session (the request session dies
    # when the request completes).
    background_tasks.add_task(_probe_server_background, mcp.id)
    return mcp.to_dict()


@router.get("/mcp/servers")
def list_mcp_servers(db: Session = Depends(get_db)):
    """List all connected MCP servers."""
    from app.models.mcp_server import McpServer
    servers = (
        db.query(McpServer)
        .filter(McpServer.is_deleted == False)
        .order_by(McpServer.created_date.desc())
        .all()
    )
    return [s.to_dict() for s in servers]


@router.delete("/mcp/servers/{server_id}")
def disconnect_mcp_server(
    server_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
):
    """Disconnect an MCP server.

    Requiring the user here is new (2026-09-17): this endpoint previously took no
    auth dependency at all, so it was reachable unauthenticated — and its
    authorization check needs the caller's role, which only exists once the user
    is resolved. (Measured the hard way: passing `user` into the check without
    adding it to the signature raises NameError -> HTTP 500, and the delete
    silently never happens.)
    """
    from app.models.mcp_server import McpServer
    server = (
        db.query(McpServer)
        .filter(McpServer.id == server_id, McpServer.is_deleted == False)
        .first()
    )
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")

    _require_mcp_access(server.name, user, action="delete")

    # Withdraw the server's tools from the agent registry first: a dangling
    # registry entry would keep the tool in every agent's tool list and answer
    # every call with "no longer connected".
    from app.services.tool_handlers.mcp_proxy import unregister_mcp_server

    unregister_mcp_server(server)

    server.status = "disconnected"
    server.is_deleted = True
    db.add(server)
    db.commit()

    # Phase 0B: enterprise orphan cleanup (org-scoped, idempotent,
    # audited). Failure is surfaced, never silently reported as clean.
    cleanup_payload: dict | None = None
    try:
        from app.services.enterprise.cleanup import handle_resource_deleted
        cleanup_payload = handle_resource_deleted(
            db, resource_type="mcp_server", resource_id=server.id,
            org_id=server.org_id, actor_id=getattr(user, "id", None),
        )
    except Exception as exc:  # noqa: BLE001 — surface, don't undo
        logger.exception("post-delete cleanup failed for MCP server %s", server.id)
        cleanup_payload = {"cleanup_error": str(exc)[:300]}
    return {"success": True, "cleanup": cleanup_payload}


@router.get("/mcp/servers/{server_id}/tools")
async def list_mcp_server_tools(server_id: str, db: Session = Depends(get_db)):
    """List tools available from a specific connected MCP server.

    Serves from the in-process cache first; if empty, probes the server live
    so the workflow canvas tool picker always has fresh options.
    """
    from app.models.mcp_server import McpServer
    server = (
        db.query(McpServer)
        .filter(McpServer.id == server_id, McpServer.is_deleted == False)
        .first()
    )
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")

    from app.services.tool_handlers.mcp_proxy import get_mcp_tools_for_server

    tools = get_mcp_tools_for_server(server.id)
    if not tools:
        # 2026-09-16: this used to call the hand-rolled probe, which skipped
        # the MCP handshake and so never got an answer from a real server.
        # Probe through the SDK client instead — it negotiates the protocol
        # and refreshes the row's tool/resource counts as a side effect.
        result = await _probe_server(db, server)
        if result.get("status") != "connected":
            return {"tools": [], "error": result.get("error", "MCP probe failed")}
        tools = get_mcp_tools_for_server(server.id)
    return {"tools": tools}


# ─── 2026-spec helpers + endpoints ──────────────────────────────────────

import asyncio
import secrets
from datetime import datetime, timezone

from app.config import settings
from app.services.mcp_client import McpConnection
from app.services.mcp_client import connect_http, connect_stdio  # noqa: F401
from app.services.mcp_client import connection_for_server
from app.services.mcp_client import oauth as oauth_mod
from app.services.mcp_client.registry import get_marketplace
from app.services.mcp_client.session import get_session_manager

# OAuth authorization state (short-lived, in-memory, per server row).
_PENDING_OAUTH: dict[str, dict] = {}


async def _connect_from_row(db: Session, server) -> McpConnection:
    """Create a McpConnection from a McpServer row (stdio or http).

    2026-09-16: delegates to the shared factory in ``app.services.mcp_client``
    so the router and the tool proxy can never drift on transport handling
    (the local copy here previously handled only ``http`` exactly, and the
    proxy handled it as a shell command — an HTTP URL passed through
    ``shlex`` is nonsense).
    """
    return await connection_for_server(server)


async def _probe_server(db: Session, server) -> dict:
    """Connect, negotiate, and capture capabilities + tool/resource counts.

    As a side effect the server's tools are published to the agent tool
    registry (one entry per tool, named ``mcp__<server>__<tool>``) so agents can
    actually call them. This is the only place a server's tool list is captured,
    so registration and the UI's cached list can never disagree.
    """
    from app.services.tool_handlers.mcp_proxy import register_mcp_tools

    conn = await _connect_from_row(db, server)
    try:
        tools = await conn.list_tools()
        resources = []
        try:
            resources = await conn.list_resources()
        except Exception:  # noqa: BLE001
            resources = []
        record = register_mcp_tools(server, [t.to_dict() for t in tools])
        server.tools_count = len(tools)
        server.resources_count = len(resources)
        server.capabilities = conn.capabilities or {}
        server.status = "connected"
        server.last_probed_at = datetime.now(timezone.utc)
        db.commit()
        return {
            "status": "connected",
            "tools_count": len(tools),
            "resources_count": len(resources),
            "capabilities": conn.capabilities or {},
            "server_info": conn.server_info or {},
            "registry_tools": list(record.registry_names),
            "tools": [t.to_dict() for t in tools][:50],
        }
    except Exception as exc:  # noqa: BLE001
        server.status = "error"
        db.commit()
        logger.warning("MCP probe failed for '%s': %s", server.name, exc)
        return {"status": "error", "error": str(exc)}
    finally:
        await conn.close()


def _probe_server_background(server_id: str) -> None:
    """Run the probe outside the request lifecycle with a fresh session."""
    from app.database import SessionLocal
    from app.models.mcp_server import McpServer

    db = SessionLocal()
    try:
        server = db.query(McpServer).filter(McpServer.id == server_id).first()
        if server is not None:
            asyncio.run(_probe_server(db, server))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Background MCP probe failed: %s", exc)
    finally:
        db.close()


@router.get("/mcp/marketplace")
def mcp_marketplace(user=Depends(get_current_user_required)) -> dict:
    """Curated MCP server catalog for the MySpace marketplace tab."""
    return {"servers": get_marketplace()}


@router.post("/mcp/servers/{server_id}/probe")
async def probe_server(
    server_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
) -> dict:
    """Re-probe a connected server (initialize + tools/list) on demand."""
    from app.models.mcp_server import McpServer

    server = db.query(McpServer).filter(
        McpServer.id == server_id, McpServer.is_deleted == False
    ).first()
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    _require_mcp_access(server.name, user)
    return await _probe_server(db, server)


@router.post("/mcp/servers/{server_id}/oauth/start")
async def oauth_start(
    server_id: str,
    req: OAuthStartRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
) -> dict:
    """Start the OAuth authorization-code flow (PKCE) for a remote server."""
    from app.models.mcp_server import McpServer

    server = db.query(McpServer).filter(
        McpServer.id == server_id, McpServer.is_deleted == False
    ).first()
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    _require_mcp_access(server.name, user)
    base = (server.base_url or server.server_url).rstrip("/")
    metadata = await oauth_mod.fetch_metadata(base)
    if not metadata.get("authorization_endpoint"):
        raise HTTPException(
            status_code=400,
            detail="Server does not advertise OAuth metadata (.well-known/oauth-authorization-server)",
        )
    verifier, challenge = oauth_mod.generate_pkce()
    state = secrets.token_urlsafe(16)
    redirect_uri = (
        f"{settings.FRONTEND_URL.rstrip('/')}/my-space?tab=mcp"
        f"&oauth=callback&server={server.id}"
    )
    auth_url = oauth_mod.build_authorize_url(
        metadata,
        client_id=req.client_id,
        redirect_uri=redirect_uri,
        scopes=req.scopes or oauth_mod.DEFAULT_SCOPES,
        state=state,
        code_challenge=challenge,
    )
    _PENDING_OAUTH[server.id] = {
        "state": state,
        "code_verifier": verifier,
        "metadata": metadata,
        "redirect_uri": redirect_uri,
        "client_id": req.client_id,
        "client_secret": req.client_secret or "",
    }
    return {"auth_url": auth_url, "state": state, "redirect_uri": redirect_uri}


@router.post("/mcp/servers/{server_id}/oauth/callback")
async def oauth_callback(
    server_id: str,
    req: OAuthCallbackRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
) -> dict:
    """Exchange the authorization code and persist tokens on the row."""
    from app.models.mcp_server import McpServer

    pending = _PENDING_OAUTH.get(server_id)
    if not pending or pending.get("state") != req.state:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
    server = db.query(McpServer).filter(
        McpServer.id == server_id, McpServer.is_deleted == False
    ).first()
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    _require_mcp_access(server.name, user)
    try:
        tokens = await oauth_mod.exchange_code(
            pending["metadata"],
            client_id=pending["client_id"],
            client_secret=pending["client_secret"],
            code=req.code,
            redirect_uri=pending["redirect_uri"],
            code_verifier=pending["code_verifier"],
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"OAuth token exchange failed: {exc}") from exc
    server.oauth_token_json = oauth_mod.tokens_to_json(tokens)
    server.auth_type = "oauth"
    db.commit()
    _PENDING_OAUTH.pop(server_id, None)
    return {"success": True, "auth_type": "oauth"}


@router.post("/mcp/servers/{server_id}/tools/{tool_name}/call")
async def call_server_tool(
    server_id: str,
    tool_name: str,
    req: McpToolCallRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
) -> dict:
    """Call a tool on a connected server via a persistent session."""
    from app.models.mcp_server import McpServer

    server = db.query(McpServer).filter(
        McpServer.id == server_id, McpServer.is_deleted == False
    ).first()
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    _require_mcp_access(server.name, user)
    manager = get_session_manager()
    try:
        conn = await manager.get(server.id, _connect_from_row, db, server)
        return await conn.call_tool(tool_name, req.arguments or {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("MCP tool call %s/%s failed: %s", server.name, tool_name, exc)
        return {"success": False, "error": str(exc)}


@router.post("/mcp/servers/{server_id}/refresh")
async def refresh_server_tools(
    server_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
) -> dict:
    """Re-probe and update a server's tool/resource counts."""
    from app.models.mcp_server import McpServer

    server = db.query(McpServer).filter(
        McpServer.id == server_id, McpServer.is_deleted == False
    ).first()
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    _require_mcp_access(server.name, user)
    result = await _probe_server(db, server)
    if result.get("status") == "error":
        raise HTTPException(status_code=502, detail=result.get("error", "probe failed"))
    return result


@router.patch("/mcp/servers/{server_id}/grants")
async def update_mcp_grants(
    server_id: str,
    req: McpGrantsRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
) -> dict:
    """Set which agents may use this server's tools ([] = every agent).

    Applied to the live registration immediately — waiting for the next probe
    would leave the setting silently inert for as long as the server stayed
    connected.
    """
    from app.models.mcp_server import McpServer
    from app.services.tool_handlers.mcp_proxy import refresh_grants

    server = db.query(McpServer).filter(
        McpServer.id == server_id, McpServer.is_deleted == False
    ).first()
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    _require_mcp_access(server.name, user, action="grant")

    server.granted_agents = list(req.granted_agents or [])
    db.commit()
    db.refresh(server)
    refresh_grants(server, server.granted_agents)
    return server.to_dict()


async def rehydrate_registered_tools() -> dict:
    """Re-probe connected servers so their tools survive a restart.

    2026-09-17: registration lives in process memory, so every backend restart
    silently emptied the agent tool lists of MCP tools while the DB still said
    ``status: connected`` — the UI showed a healthy server whose tools no agent
    could see. Runs in the background at startup; failures are per-server and
    never block serving traffic.
    """
    from app.database import SessionLocal
    from app.models.mcp_server import McpServer

    db = SessionLocal()
    restored: dict[str, Any] = {}
    try:
        rows = (
            db.query(McpServer)
            .filter(McpServer.is_deleted == False)  # noqa: E712
            .all()
        )
        for server in rows:
            if (server.status or "").lower() != "connected":
                continue
            try:
                result = await _probe_server(db, server)
                restored[server.name] = result.get("registry_tools") or []
            except Exception as exc:  # noqa: BLE001
                logger.warning("MCP rehydrate failed for '%s': %s", server.name, exc)
    finally:
        db.close()
    if restored:
        logger.info("MCP tools rehydrated for %d server(s): %s", len(restored), restored)
    return restored
