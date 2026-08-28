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

export function deleteDashboardApp(slugOrId) {
  return authFetch(`${BASE}/app-records/${encodeURIComponent(slugOrId)}`, { method: 'DELETE' }).then(j);
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

// ── Full-stack dashboard app records (new pipeline) ──
// The app frontends are served as static files at `${BASE}/apps/{slug}/`
// (mounted by DashboardAppManager). These JSON record endpoints use the
// `/app-records/` prefix to avoid colliding with that static mount.
export function getDashboardApp(slug) {
  return authFetch(`${BASE}/app-records/${slug}`, { method: 'GET' }).then(j);
}

export function listDashboardApps() {
  return authFetch(`${BASE}/app-records`, { method: 'GET' }).then(j);
}

export function markDashboardViewed(slug) {
  return authFetch(`${BASE}/app-records/${slug}/mark-viewed`, { method: 'POST' }).then(j);
}

export function getDashboardChatThread(slug) {
  return authFetch(`${BASE}/app-records/${slug}/chat-thread`, { method: 'GET' }).then(j);
}
