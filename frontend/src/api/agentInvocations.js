// Agent observability API helpers — admin-only surfaces backed by
// backend/app/routers/admin_invocations.py.
import { authFetch } from '@/api/authFetch';

export async function listInvocations(params = {}) {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') qs.set(k, v);
  });
  const q = qs.toString();
  const resp = await authFetch(`/api/admin/invocations${q ? `?${q}` : ''}`);
  if (!resp.ok) throw new Error(`Failed to load invocations (${resp.status})`);
  return resp.json();
}

export async function getInvocationSummary(params = {}) {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') qs.set(k, v);
  });
  const q = qs.toString();
  const resp = await authFetch(`/api/admin/invocations/summary${q ? `?${q}` : ''}`);
  if (!resp.ok) throw new Error(`Failed to load summary (${resp.status})`);
  return resp.json();
}

export async function getInvocationDetail(id) {
  const resp = await authFetch(`/api/admin/invocations/${id}`);
  if (!resp.ok) throw new Error(`Failed to load invocation (${resp.status})`);
  return resp.json();
}
