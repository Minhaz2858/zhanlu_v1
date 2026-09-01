import { authFetch } from '@/api/authFetch';
import { appParams } from '@/lib/app-params';

const APP_ID = appParams.appId || 'local-zhanlu-app';

function base(projectId) {
  return `/api/apps/${APP_ID}/projects/${projectId}`;
}

export async function listCatalogTables(projectId, search = '') {
  const url = `${base(projectId)}/catalog/tables${search ? `?search=${encodeURIComponent(search)}` : ''}`;
  const resp = await authFetch(url);
  if (!resp.ok) throw new Error('Failed to load catalog tables');
  return resp.json();
}

export async function putCatalogOverlay(projectId, payload) {
  const resp = await authFetch(`${base(projectId)}/catalog/overlay`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) throw new Error('Failed to save overlay');
  return resp.json();
}

export async function listCatalogEntities(projectId) {
  const resp = await authFetch(`${base(projectId)}/catalog/entities`);
  if (!resp.ok) throw new Error('Failed to load entities');
  return resp.json();
}

export async function listRegistryResources(projectId, resourceType = '') {
  const url = `${base(projectId)}/registry/resources${resourceType ? `?resource_type=${resourceType}` : ''}`;
  const resp = await authFetch(url);
  if (!resp.ok) throw new Error('Failed to load registry resources');
  return resp.json();
}

export async function getProjectKnowledgeMap(projectId) {
  const resp = await authFetch(`${base(projectId)}/knowledge-map`);
  if (!resp.ok) throw new Error('Failed to load project knowledge map');
  return resp.json();
}

export async function listCatalogMetrics(projectId) {
  const resp = await authFetch(`${base(projectId)}/catalog/metrics`);
  if (!resp.ok) throw new Error('Failed to load metrics');
  return resp.json();
}

export async function putCatalogMetric(projectId, metricId, payload) {
  const resp = await authFetch(`${base(projectId)}/catalog/metrics/${metricId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) throw new Error('Failed to save metric');
  return resp.json();
}

export async function bootstrapCatalogMetrics(projectId) {
  const resp = await authFetch(`${base(projectId)}/catalog/metrics/bootstrap`, {
    method: 'POST',
  });
  if (!resp.ok) throw new Error('Failed to bootstrap metrics');
  return resp.json();
}
