/**
 * Pure data helpers for the AgentToolsPanel. Kept in a separate
 * JS-less file so they can be unit-tested under vitest's node
 * environment (no jsdom, no shadcn imports, no window).
 *
 * The contract for what shows up in the panel lives in three places
 * (keep in sync):
 *   - backend/app/services/tool_registry.py (DEFAULT_USER_AGENT_TOOLS, SKILL_DISPLAY_TO_TOOL_NAME)
 *   - backend/app/services/agent_tools.py (_create_agent fallback path)
 *   - this file (DEFAULT_USER_AGENT_TOOLS, SKILL_DISPLAY_TO_TOOL)
 */

// Mirrors backend/app/services/tool_registry.py:DEFAULT_USER_AGENT_TOOLS.
// Order matters — the UI renders tools in this order, and the contract
// test in test_user_agent_tool_fallback.py pins the order.
//
// 4-tool anti-hallucination baseline: web_search and web_extract are
// pre-enabled so every new agent can ground time-sensitive / external
// questions in live data. memory and todo remain for state and planning.
export const DEFAULT_USER_AGENT_TOOLS = ['web_search', 'web_extract', 'memory', 'todo'];

// Mirrors SKILL_DISPLAY_TO_TOOL_NAME in backend/app/services/tool_registry.py.
// A subset is sufficient for the panel's display; the full mapping lives
// on the backend and is used during agent creation.
export const SKILL_DISPLAY_TO_TOOL = {
  'Web Search': 'web_search',
  'Web Extract': 'web_extract',
  'Memory': 'memory',
  'Todo': 'todo',
  'Read File': 'read_file',
  'Write File': 'write_file',
  'Image Generation': 'image_generation',
  'Code Execution': 'execute_code',  // legacy alias — older agents
  'Code Executor': 'execute_code',  // canonical — matches backend seed.py
  'Delegate Task': 'delegate_task',
  'Database Query': 'ask_data_agent',
  'Sandbox Skill': 'run_sandbox_skill',
};

/**
 * Given an agent, return the two lists the panel renders: tools that
 * came from the user's skill selections, and the baseline tools that
 * get added automatically.
 *
 * Pure function (no side effects, no imports beyond this file).
 */
export function resolveTools(agent) {
  // Defensive: agent may be null, or its skills field may be missing /
  // not-an-array (e.g. legacy data where it was a JSON string). The
  // function is positioned as a stable contract, so guard at the edge.
  const skillNames =
    agent && Array.isArray(agent.skills) ? agent.skills : [];
  const mapped = skillNames
    .map((displayName) => SKILL_DISPLAY_TO_TOOL[displayName])
    .filter(Boolean);
  // De-dupe preserving input order.
  const orderedMapped = [...new Set(mapped)];
  // Baseline is DEFAULT minus anything already in mapped.
  const baseline = DEFAULT_USER_AGENT_TOOLS.filter(
    (t) => !orderedMapped.includes(t),
  );
  return { mapped: orderedMapped, baseline };
}
