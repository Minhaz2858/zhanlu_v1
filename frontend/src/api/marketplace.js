import { authFetch } from '@/api/authFetch';

const BASE = '/api/marketplace';

export async function listSources() {
  const resp = await authFetch(`${BASE}/sources`);
  if (!resp.ok) throw new Error('Failed to list sources');
  return resp.json();
}

export async function addSource({ url, name, source_type, description, icon_emoji }) {
  const resp = await authFetch(`${BASE}/sources`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, name, source_type, description, icon_emoji }),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.detail?.message || 'Failed to add source');
  }
  return resp.json();
}

export async function deleteSource(sourceId, { force = false } = {}) {
  // ``force=true`` upgrades the default-source delete from a soft
  // hide (is_hidden=True) to a hard delete that fires the CASCADE
  // on external_skills. The URL is also recorded as a tombstone
  // in ``removed_curated_urls`` so the seed won't re-create the
  // source on subsequent list calls (the user reported
  // "after refresh it showing again" on 2026-07-29). The user can
  // restore the source later via ``restoreRemovedSource``.
  const url = force
    ? `${BASE}/sources/${sourceId}?force=true`
    : `${BASE}/sources/${sourceId}`;
  const resp = await authFetch(url, { method: 'DELETE' });
  if (!resp.ok) throw new Error('Failed to delete source');
  return resp.json();
}

export async function listRemovedSources() {
  // Returns the list of curated source URLs the user has explicitly
  // deleted. The marketplace tab shows a "Show removed (N)" toggle
  // for this list, with a "Restore" button per entry.
  const resp = await authFetch(`${BASE}/sources/removed`);
  if (!resp.ok) throw new Error('Failed to list removed sources');
  return resp.json();
}

export async function restoreRemovedSource(url) {
  // Clear the tombstone for a previously-deleted curated source.
  // The endpoint re-seeds the source so the user sees it back in the
  // list immediately (no need to wait for the next list call).
  // ``url`` is the original source URL — the API takes it as a path
  // param so the user doesn't need to remember the source id (which
  // is gone after the hard delete).
  const encoded = encodeURIComponent(url);
  const resp = await authFetch(`${BASE}/sources/removed/${encoded}/restore`, {
    method: 'POST',
  });
  if (!resp.ok) throw new Error('Failed to restore source');
  return resp.json();
}

export async function syncSource(sourceId) {
  const resp = await authFetch(`${BASE}/sources/${sourceId}/sync`, { method: 'POST' });
  if (!resp.ok) throw new Error('Failed to sync source');
  return resp.json();
}

export async function listSourceSkills(sourceId, { q, sort, limit, offset } = {}) {
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (sort) params.set('sort', sort);
  if (limit) params.set('limit', limit);
  if (offset) params.set('offset', offset);
  const resp = await authFetch(`${BASE}/sources/${sourceId}/skills?${params}`);
  if (!resp.ok) throw new Error('Failed to list skills');
  return resp.json();
}

export async function getSkill(skillId) {
  const resp = await authFetch(`${BASE}/skills/${skillId}`);
  if (!resp.ok) throw new Error('Failed to get skill');
  return resp.json();
}

export async function installSkill(skillId) {
  const resp = await authFetch(`${BASE}/skills/${skillId}/install`, { method: 'POST' });
  if (!resp.ok) throw new Error('Failed to install skill');
  return resp.json();
}

export async function listMySkills() {
  const resp = await authFetch(`${BASE}/my-skills`);
  if (!resp.ok) throw new Error('Failed to list my skills');
  return resp.json();
}

export async function removeMySkill(toolId) {
  const resp = await authFetch(`${BASE}/my-skills/${toolId}`, { method: 'DELETE' });
  if (!resp.ok) throw new Error('Failed to remove skill');
  return resp.json();
}

export async function listCurated() {
  const resp = await authFetch(`${BASE}/curated`);
  if (!resp.ok) throw new Error('Failed to list curated');
  return resp.json();
}
