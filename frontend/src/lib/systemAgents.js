// Single source of truth for "this is a platform-shipped agent".
//
// System agents are seeded by the backend (see
// backend/app/services/system_agents.py) and marked with
// ``is_system=True`` in the DB. The frontend hides them from
// user-facing lists (My Space → Agents tab, the agent picker,
// the active-agent chip) but the runtime still uses them —
// in particular ``general_assistant`` is auto-selected silently
// for any chat with no user-picked agent, so Ungrouped sessions
// still get a date anchor and a real-time toolset.
//
// We accept either the backend ``is_system`` field OR a name
// match against this constant. The name-match is the fallback
// for legacy rows (pre-migration) and for any code path that
// constructs an AgentApp dict locally without round-tripping
// the database. Keeping both checks makes the UI robust to
// stale data.

// Imported for filterUserAgents below: workflows and agents share the
// `agent_apps` table, so the agents filter must also drop workflow rows.
import { isWorkflowFlow } from '@/lib/workflowGraph';

export const SYSTEM_AGENT_NAMES = Object.freeze([
  'agent_builder',
  'skill_agent',
  'automation_agent',
  'general_assistant',
  'power_user',
  // data_agent is code-only (BUILTIN_AGENTS, not a row in
  // agent_apps) and is never returned by the list endpoint, but
  // include it for defense in depth in case a future change
  // surfaces it.
  'data_agent',
]);

const _systemNameSet = new Set(SYSTEM_AGENT_NAMES);

/**
 * @param {object | null | undefined} agent
 * @returns {boolean} true if the agent should be hidden from the user.
 */
export function isSystemAgent(agent) {
  if (!agent) return false;
  // Backend source of truth — preferred.
  if (agent.is_system === true) return true;
  // Legacy / fallback — match by name. This keeps the UI
  // correct even on databases that haven't yet been migrated
  // to add the is_system column.
  if (typeof agent.name === 'string' && _systemNameSet.has(agent.name)) {
    return true;
  }
  return false;
}

/**
 * Filter a list of agents, dropping system agents AND Agent Workflow rows.
 * Returns a new array; does not mutate the input.
 *
 * Agent Workflows are ordinary `agent_apps` rows with `flow_mode=true` and a
 * canvas-shaped `flow` ({ version, nodes, edges }) — the backend
 * `GET /workflows` list is their real home (My Space → Agent Workflow tab).
 * They used to leak into every Agents list because this filter only dropped
 * system agents, and following such a row to `/my-space/agent/:id` rendered
 * the agent flow tree over a canvas object → page crash
 * ("flow.forEach is not a function"). Agents that use the composable-flow
 * mode store an ARRAY in `flow`, so the shape test (not the flag) is what
 * distinguishes them.
 *
 * @template T
 * @param {T[]} agents
 * @returns {T[]}
 */
export function filterUserAgents(agents) {
  if (!Array.isArray(agents)) return [];
  return agents.filter((a) => !isSystemAgent(a) && !isWorkflowFlow(a?.flow));
}
