import { authFetch } from '@/api/authFetch';
import { base44 } from '@/api/base44Client';
import { appParams } from '@/lib/app-params';
import { coerceStringArray } from '@/lib/jsonArray';

const APP_ID = () => appParams.appId || 'default-app';

async function ensureOk(resp, fallback) {
  if (resp.ok) return resp;
  let detail = fallback;
  try {
    const body = await resp.json();
    detail = body.detail || fallback;
  } catch {
    /* keep fallback */
  }
  throw new Error(detail);
}

/**
 * List existing access policies for a (resource, user) pair.
 */
export async function listPolicies(resourceType, resourceId, userId) {
  const resp = await authFetch(
    `/api/access-policies?resource_type=${resourceType}&resource_id=${resourceId}&user_id=${userId}`
  );
  await ensureOk(resp, 'Failed to load access policies');
  const data = await resp.json();
  return data.policies || [];
}

/**
 * Batch-upsert the full policy matrix for a (resource, user) pair.
 */
export async function savePolicies(resourceType, resourceId, userId, policies) {
  const resp = await authFetch('/api/access-policies', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      resource_type: resourceType,
      resource_id: resourceId,
      user_id: userId,
      policies,
    }),
  });
  await ensureOk(resp, 'Failed to save access policies');
  const data = await resp.json();
  return data.policies || [];
}

/**
 * Delete a single policy row.
 */
export async function deletePolicy(policyId) {
  const resp = await authFetch(`/api/access-policies/${policyId}`, {
    method: 'DELETE',
  });
  await ensureOk(resp, 'Failed to delete policy');
  return resp.json();
}

/**
 * Preview effective permissions for a user on a resource.
 */
export async function previewPermissions(resourceType, resourceId, userId) {
  const resp = await authFetch(
    `/api/access-policies/preview?resource_type=${resourceType}&resource_id=${resourceId}&user_id=${userId}`
  );
  await ensureOk(resp, 'Failed to preview permissions');
  return resp.json();
}

/**
 * List the KnowledgeBases bound to a project or agent, as [{ id, name }].
 */
export async function listBoundKbs(resourceType, resourceId) {
  if (resourceType === 'project') {
    const resp = await authFetch(
      `/api/apps/${APP_ID()}/projects/${resourceId}/knowledge-bases?limit=200`
    );
    await ensureOk(resp, 'Failed to load databases');
    const kbs = await resp.json();
    return (Array.isArray(kbs) ? kbs : []).map((kb) => ({
      id: kb.id,
      name: kb.name || kb.id,
    }));
  }

  // agent: read the agent's bound knowledge_bases JSON array, then resolve names
  const agentResp = await authFetch(
    `/api/apps/${APP_ID()}/entities/AgentApp/${resourceId}`
  );
  await ensureOk(agentResp, 'Failed to load agent');
  const agent = await agentResp.json();
  const kbIds = coerceStringArray(agent?.knowledge_bases);

  const out = [];
  for (const kbId of kbIds) {
    try {
      const kb = await base44.entities.KnowledgeBase.get(kbId);
      out.push({ id: kb.id, name: kb.name || kb.id });
    } catch {
      out.push({ id: kbId, name: kbId });
    }
  }
  return out;
}

/**
 * Fetch the catalog tables (and their columns) for a database KB.
 * Returns [{ name, columns: [] }]. Returns [] for non-database / unindexed KBs.
 */
export async function listKbTables(kbId) {
  try {
    const resp = await authFetch(
      `/api/apps/${APP_ID()}/knowledge_bases/${kbId}/catalog/tables`
    );
    if (!resp.ok) return [];
    const data = await resp.json();
    return (data.tables || []).map((t) => ({
      name: t.table_name,
      columns: Array.isArray(t.column_names) ? t.column_names : [],
    }));
  } catch {
    return [];
  }
}
