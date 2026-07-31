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
