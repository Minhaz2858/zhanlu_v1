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
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_required

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp"])


# ─── Request / Response Models ──────────────────────────────────────────

class McpConnectRequest(BaseModel):
    """Paste a Claude-style MCP server config."""
    name: str
    command: str
    args: Optional[list[str]] = None
    env: Optional[dict[str, str]] = None
    description: Optional[str] = None
    transport: str = "stdio"  # stdio, sse, streamable


class McpServerResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    transport: str
    status: str
    tools_count: int
    resources_count: int


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
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
):
    """Connect to an external MCP server by pasting its config.

    Expected config shape (same as Claude Desktop):
    ```
    {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_TOKEN": "..."}
    }
    ```
    """
    from app.models.mcp_server import McpServer

    # Check for duplicate name
    existing = (
        db.query(McpServer)
        .filter(
            McpServer.name == req.name,
            McpServer.is_deleted == False,
        )
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail=f"MCP server '{req.name}' already connected")

    # Build server URL from command + args (simplified — full process
    # management is done in the proxy handler at invocation time)
    server_url = f"{req.command} {' '.join(req.args or [])}".strip()

    mcp = McpServer(
        name=req.name,
        description=req.description,
        server_url=server_url,
        transport=req.transport,
        status="connected",
        tools_count=0,
        resources_count=0,
    )
    db.add(mcp)
    db.commit()
    db.refresh(mcp)

    # Try to probe the server for initial tool count
    _probe_mcp_tools_async(mcp.id, db, req.command, req.args, req.env)

    return mcp.to_dict()


def _probe_mcp_tools_async(
    server_id: str,
    db: Session,
    command: str,
    args: list[str] | None,
    env: dict[str, str] | None,
):
    """Probe an MCP server for its tools in a best-effort manner.

    This runs synchronously on the import path; a production version
    would use a background task or subprocess management.
    """
    try:
        from app.services.tool_handlers.mcp_proxy import probe_mcp_server
        result = probe_mcp_server(command, args or [], env or {})
        if result:
            mcp = db.query(db.bind.registry._class_registry.get("McpServer")).get(server_id) if False else None
            # For now, just log the result
            logger.info("MCP probe result for %s: %d tools", command, len(result.get("tools", [])))
    except Exception as exc:
        logger.debug("MCP probe skipped for %s: %s", command, exc)


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
def disconnect_mcp_server(server_id: str, db: Session = Depends(get_db)):
    """Disconnect an MCP server."""
    from app.models.mcp_server import McpServer
    server = (
        db.query(McpServer)
        .filter(McpServer.id == server_id, McpServer.is_deleted == False)
        .first()
    )
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")

    server.status = "disconnected"
    server.is_deleted = True
    db.add(server)
    db.commit()
    return {"success": True}


@router.get("/mcp/servers/{server_id}/tools")
def list_mcp_server_tools(server_id: str, db: Session = Depends(get_db)):
    """List tools available from a specific connected MCP server."""
    from app.models.mcp_server import McpServer
    server = (
        db.query(McpServer)
        .filter(McpServer.id == server_id, McpServer.is_deleted == False)
        .first()
    )
    if not server:
        raise HTTPException(status_code=404, detail="MCP server not found")

    # Return the tools registered for this server from the proxy
    from app.services.tool_handlers.mcp_proxy import get_mcp_tools_for_server
    return {"tools": get_mcp_tools_for_server(server.name)}
