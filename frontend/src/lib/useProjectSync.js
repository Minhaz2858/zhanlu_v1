import { useState, useEffect, useCallback, useMemo } from 'react';
import { base44 } from '@/api/base44Client';

/**
 * useProjectSync — tiny hook that bridges the legacy `project` name-string
 * field and the new `project_id` FK on AgentApp / KnowledgeBase / etc.
 *
 * Why this exists: previously the codebase wrote only `project` (a name
 * string like "enterprise" or "global") to associate an entity with a
 * Project. The Project Detail page now filters by `project_id` (the FK),
 * which left old rows invisible. This hook keeps both fields in sync:
 * whenever `project` changes (whether it's set to an id, a name, or the
 * "global" sentinel) we resolve to a (project_id, project_name) pair and
 * emit BOTH, so legacy readers and new readers both see the change.
 *
 * Many-to-many memberships (project_agents):
 *   The `syncProjectMembership` helper mirrors the AgentApp.project_id
 *   change into the new `project_agents` association table so the agent
 *   appears in the right project's membership list. The legacy
 *   `project_id` is kept as the agent's "primary/home" project so
 *   backward-compatible code keeps working.
 *
 * Usage:
 *   const { resolveProjectChange, syncProjectMembership } = useProjectSync();
 *   <ProjectSelector value={form.project || 'global'} onChange={(v) => {
 *     resolveProjectChange(v, update);                      // updates form with project_id + project
 *     syncProjectMembership(form.id, v);                    // also syncs project_agents table
 *   }} />
 */
export function useProjectSync() {
  const [projects, setProjects] = useState([]);

  useEffect(() => {
    let cancelled = false;
    // Load ALL projects so legacy rows with status=null still resolve
    // correctly when written through the picker.
    base44.entities.Project.list('-updated_date', 500)
      .then((list) => { if (!cancelled) setProjects(list || []); })
      .catch(() => { if (!cancelled) setProjects([]); });
    return () => { cancelled = true; };
  }, []);

  // Build a quick name → id lookup.
  const byName = useMemo(() => {
    const m = new Map();
    for (const p of projects) m.set(p.name, p);
    return m;
  }, [projects]);

  const byId = useMemo(() => {
    const m = new Map();
    for (const p of projects) m.set(p.id, p);
    return m;
  }, [projects]);

  /**
   * resolveProjectChange — given a value coming out of ProjectSelector
   * (could be a project id, a project name, the 'global' sentinel, or
   * null/''), return `{ project_id, project }` to write to the entity.
   *
   * The matcher is forgiving: it falls back from id to name to handle
   * whichever contract ProjectSelector is using (legacy name or new id).
   */
  const resolve = useCallback((value) => {
    if (!value || value === '' || value === 'all') {
      return { project_id: null, project: 'global' };
    }
    if (value === 'global') {
      return { project_id: null, project: 'global' };
    }
    // First try treating as id
    const byIdHit = byId.get(value);
    if (byIdHit) {
      return { project_id: byIdHit.id, project: byIdHit.name };
    }
    // Then as name
    const byNameHit = byName.get(value);
    if (byNameHit) {
      return { project_id: byNameHit.id, project: byNameHit.name };
    }
    // Unknown value — store as name string in legacy field, no FK
    return { project_id: null, project: value };
  }, [byId, byName]);

  /**
   * resolveProjectChange — convenience: takes a value AND an `update`
   * setter (from useAgentBuilder-style state hooks), and writes both
   * fields in one pass.
   */
  const resolveProjectChange = useCallback((value, update) => {
    const { project_id, project } = resolve(value);
    update({ project_id, project });
  }, [resolve]);

  /**
   * syncProjectMembership — mirror an AgentApp's project_id change into
   * the new `project_agents` association table.
   *
   * Behavior:
   *   - When the new project is null / 'global': this is a no-op. The
   *     legacy `project_id` is being cleared, but the agent may still
   *     legitimately be a member of other projects via its other
   *     ProjectAgent rows. We do NOT bulk-delete every membership.
   *   - When the new project is a real id: ensure a ProjectAgent row
   *     exists for (newProjectId, agentId). This is the membership that
   *     will be shown on the project's Agents list.
   *
   * This is fire-and-forget — callers shouldn't await it for UX; we
   * catch our own errors so a membership failure doesn't break the
   * save flow.
   */
  const syncProjectMembership = useCallback(async (agentId, newProjectValue, opts = {}) => {
    if (!agentId) return;
    const { project_id: newProjectId } = resolve(newProjectValue);
    try {
      if (newProjectId) {
        // Check for existing membership row (unique on project+agent+org+app)
        const existing = await base44.entities.ProjectAgent
          .filter({ project_id: newProjectId, agent_id: agentId })
          .catch(() => []);
        if (!Array.isArray(existing) || existing.length === 0) {
          try {
            await base44.entities.ProjectAgent.create({
              project_id: newProjectId,
              agent_id: agentId,
              role: 'primary',
            });
          } catch (e) {
            // Race condition or already-exists — ignore.
            console.warn('ProjectAgent membership create skipped:', e);
          }
        }
      }
      // When clearing the primary, we intentionally do NOT delete the
      // other-project memberships. The agent stays a member of any
      // other projects it was explicitly added to via the dialog.
    } catch (e) {
      console.warn('syncProjectMembership failed (non-fatal):', e);
    }
  }, [resolve]);

  return { resolve, resolveProjectChange, projects, syncProjectMembership };
}
