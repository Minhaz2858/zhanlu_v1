import { authFetch } from '@/api/authFetch';
import { appParams } from '@/lib/app-params';

// Workflow-agent API — canvas CRUD + validate/run/publish/runs.
// Backend router: /api/apps/{app_id}/workflows (app_id is URL cosmetics).
const appId = appParams.appId || 'default-app';
const BASE = `/api/apps/${appId}/workflows`;

async function j(res) {
  if (!res.ok) {
    let detail = `workflows API ${res.status}`;
    try {
      const body = await res.json();
      if (Array.isArray(body.detail)) detail = body.detail.join('; ');
      else if (body.detail) detail = body.detail;
    } catch { /* keep status message */ }
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

function json(method, body) {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
}

export function listWorkflows() {
  return authFetch(BASE, { method: 'GET' }).then(j);
}

export function createWorkflow() {
  return authFetch(BASE, { method: 'POST' }).then(j);
}

export function getWorkflow(id) {
  return authFetch(`${BASE}/${id}`, { method: 'GET' }).then(j);
}

export function saveWorkflow(id, { name, description, flow }) {
  return authFetch(`${BASE}/${id}`, json('PUT', { name, description, flow })).then(j);
}

export function validateWorkflow(id) {
  return authFetch(`${BASE}/${id}/validate`, { method: 'POST' }).then(j);
}

export function runWorkflow(id, input) {
  return authFetch(`${BASE}/${id}/run`, json('POST', { input })).then(j);
}

// Kick off a background run — returns { run_id } immediately; stream live
// events via streamUrl(id, runId) (n8n push pattern).
export function runWorkflowAsync(id, input) {
  return authFetch(`${BASE}/${id}/run-async`, json('POST', { input })).then(j);
}

export function streamUrl(id, runId) {
  return `${BASE}/${id}/runs/${runId}/stream`;
}

export function listWorkflowRuns(id) {
  return authFetch(`${BASE}/${id}/runs`, { method: 'GET' }).then(j);
}

// Test a single node synchronously — resolves to
// { status: 'completed'|'failed', node_id, node_type, output, error }.
// payload = { trigger_input: string, upstreams: object }.
export function testNode(workflowId, nodeId, payload) {
  return authFetch(`${BASE}/${workflowId}/nodes/${nodeId}/test`, json('POST', payload)).then(j);
}

// Re-run a workflow from a given node — returns { run_id } immediately; the
// new run streams via streamUrl(workflowId, runId).
export function rerunWorkflow(workflowId, { run_id, start_node_id, input }) {
  return authFetch(`${BASE}/${workflowId}/rerun`, json('POST', { run_id, start_node_id, input })).then(j);
}

export function publishWorkflow(id) {
  return authFetch(`${BASE}/${id}/publish`, { method: 'POST' }).then(j);
}

export function deleteWorkflow(id) {
  return authFetch(`${BASE}/${id}`, { method: 'DELETE' }).then(j);
}

// Schema registry (n8n-style INodeTypeDescription) — drives palette, forms,
// typed connections. One registry shared with the backend.
export function fetchNodeTypes() {
  return authFetch(`${BASE}/node-types`, { method: 'GET' }).then(j);
}
