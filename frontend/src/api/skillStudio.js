/**
 * Skill Studio API client — wraps all /api/skills/* and /api/skill-studio/* endpoints.
 * Uses authFetch for automatic Bearer token + 401 refresh-retry.
 */
import authFetch from '@/api/authFetch';

/**
 * Collect a skill from a web URL using agent-browser.
 * @param {string} url - The URL to scrape
 * @param {string} [skillName] - Optional skill name override
 * @returns {Promise<Object>} { success, skill_name, skill_path, scan_findings, source_url }
 */
export async function collectSkill(url, skillName) {
  const resp = await authFetch('/api/skills/collect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, skill_name: skillName || undefined }),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({ detail: 'Collection failed' }));
    throw new Error(detail.detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

/**
 * List skill execution records (SkillRun) with optional filters.
 * @param {Object} [params] - { skill_name, status, limit, offset }
 * @returns {Promise<Object>} { success, total, count, offset, limit, executions }
 */
export async function listExecutions(params = {}) {
  const qs = new URLSearchParams();
  if (params.skill_name) qs.set('skill_name', params.skill_name);
  if (params.status) qs.set('status', params.status);
  if (params.limit) qs.set('limit', String(params.limit));
  if (params.offset) qs.set('offset', String(params.offset));
  const resp = await authFetch(`/api/skills/executions?${qs.toString()}`);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

/**
 * List execution records for a specific skill.
 * @param {string} skillName
 * @param {Object} [params] - { limit, offset }
 * @returns {Promise<Object>} { success, skill_name, total, count, executions }
 */
export async function listSkillExecutions(skillName, params = {}) {
  const qs = new URLSearchParams();
  if (params.limit) qs.set('limit', String(params.limit));
  if (params.offset) qs.set('offset', String(params.offset));
  const resp = await authFetch(`/api/skills/${encodeURIComponent(skillName)}/executions?${qs.toString()}`);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

/**
 * Trigger a dry-run validation gate for a skill.
 * @param {string} skillName
 * @returns {Promise<Object>} { passed, test_case_id, result, error, checks }
 */
export async function triggerDryRun(skillName) {
  const resp = await authFetch(`/api/skills/${encodeURIComponent(skillName)}/dry-run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

/**
 * Fetch the active SkillDraft for a conversation (live folder tree).
 * Returns null when there is no draft in flight.
 * @param {string} conversationId
 * @returns {Promise<Object|null>} draft dict, or null when none exists
 */
export async function getSkillDraft(conversationId) {
  const resp = await authFetch(`/api/skill-studio/drafts/${encodeURIComponent(conversationId)}`);
  if (resp.status === 404) return null;
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const data = await resp.json();
  return data.draft || null;
}

/**
 * Save back a single file (SKILL.md or references/*.md) in an active draft.
 * @param {string} conversationId
 * @param {string} path - "SKILL.md" or "references/<filename>"
 * @param {string} content
 * @returns {Promise<Object>} { draft } — the updated draft dict
 */
export async function updateSkillDraftFile(conversationId, path, content) {
  const resp = await authFetch(`/api/skill-studio/drafts/${encodeURIComponent(conversationId)}/file`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, content }),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({ detail: 'Failed to save file' }));
    throw new Error(detail.detail || `HTTP ${resp.status}`);
  }
  const data = await resp.json();
  return data.draft;
}

/**
 * Discard the active SkillDraft for a conversation.
 * @param {string} conversationId
 * @returns {Promise<Object>} { success }
 */
export async function discardSkillDraft(conversationId) {
  const resp = await authFetch(`/api/skill-studio/drafts/${encodeURIComponent(conversationId)}`, {
    method: 'DELETE',
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}
