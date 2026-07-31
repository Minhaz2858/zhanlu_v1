import { authFetch } from '@/api/authFetch';

const BASE = '/api/dashboards';

async function j(res) {
  if (!res.ok) throw new Error(`dashboards API ${res.status}`);
  if (res.status === 204) return null;
  return res.json();
}

export function listDashboards(projectId) {
  const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
  return authFetch(`${BASE}${qs}`, { method: 'GET' }).then(j);
}

export function getDashboard(id) {
  return authFetch(`${BASE}/${id}`, { method: 'GET' }).then(j);
}

export function createDashboard(body) {
  return authFetch(BASE, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(j);
}

export function deleteDashboard(id) {
  return authFetch(`${BASE}/${id}`, { method: 'DELETE' }).then(j);
}

export function queryDashboard(id, params = {}) {
  return authFetch(`${BASE}/${id}/query`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params || {}),
  }).then(j);
}

export function updateDashboard(id, body) {
  return authFetch(`${BASE}/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(j);
}

export function previewWidgetSql(id, { sql, params } = {}) {
  const body = params ? { sql, params } : { sql };
  return authFetch(`${BASE}/${id}/preview-sql`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(j);
}

export function getDashboardByConversation(conversationId) {
  return authFetch(`${BASE}/by-conversation/${conversationId}`, { method: 'GET' }).then(j);
}

export function undoDashboardEdit(id) {
  return authFetch(`${BASE}/${id}/undo`, { method: 'POST' }).then(j);
}
