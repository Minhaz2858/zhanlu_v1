import { authFetch } from '@/api/authFetch';

// External MCP server management (backend router: /api/mcp/servers).

async function j(res) {
  if (!res.ok) {
    let detail = `MCP API ${res.status}`;
    try {
      const body = await res.json();
      if (body.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch { /* keep status */ }
    throw new Error(detail);
  }
  return res.json();
}

export function listMcpServers() {
  return authFetch('/api/mcp/servers', { method: 'GET' }).then(j);
}

export function listMcpServerTools(serverId) {
  return authFetch(`/api/mcp/servers/${serverId}/tools`, { method: 'GET' }).then(j);
}

export function getMcpMarketplace() {
  return authFetch('/api/mcp/marketplace', { method: 'GET' }).then(j);
}

export function connectMcpServer(payload) {
  return authFetch('/api/mcp/servers/connect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then(j);
}

export function deleteMcpServer(serverId) {
  return authFetch(`/api/mcp/servers/${serverId}`, { method: 'DELETE' }).then(j);
}

export function probeMcpServer(serverId) {
  return authFetch(`/api/mcp/servers/${serverId}/probe`, { method: 'POST' }).then(j);
}

export function oauthStart(serverId, payload) {
  return authFetch(`/api/mcp/servers/${serverId}/oauth/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then(j);
}

export function oauthCallback(serverId, payload) {
  return authFetch(`/api/mcp/servers/${serverId}/oauth/callback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then(j);
}

export function callMcpServerTool(serverId, toolName, argumentsObj) {
  return authFetch(`/api/mcp/servers/${serverId}/tools/${toolName}/call`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ arguments: argumentsObj || {} }),
  }).then(j);
}
