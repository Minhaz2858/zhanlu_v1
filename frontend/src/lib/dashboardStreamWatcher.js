// Names of dashboard tool calls the agent can emit. Matches the tool
// registrations in `backend/app/services/tool_handlers/dashboard_tools.py`.
const DASHBOARD_TOOLS = new Set([
  'create_dashboard',
  'update_dashboard',
  'undo_dashboard_edit',
]);

/**
 * Inspect an in-flight or finalised message's tool_calls and trigger the
 * dashboard dock panel when a dashboard edit is detected.
 *
 * Pure / side-effect-light: takes the current tool_calls snapshot, the
 * current number of dashboard edits already seen this message, and two
 * callbacks (`onOpen`, `onRefresh`). Returns the new dedupe count. Designed
 * to be called from the streaming event loop in Chat.jsx.
 *
 *   const seen = useRef(0);
 *   // ...
 *   const ev = watchForDashboardEdits(m.tool_calls, seen.current, {
 *     onOpen: () => setDashboardOpen(true),
 *     onRefresh: () => dashboardViewerRef.current?.refresh?.(),
 *   });
 *   seen.current = ev;
 *
 * @param {Array} toolCalls - message.tool_calls
 * @param {number} seenCount - number of dashboard tool results already handled
 *   for this message
 * @param {{ onOpen: () => void, onRefresh: () => void }} handlers
 * @returns {number} new seenCount (= seenCount + newly-detected dashboard tool calls)
 */
export function watchForDashboardEdits(toolCalls, seenCount, handlers) {
  if (!Array.isArray(toolCalls) || toolCalls.length === 0) return seenCount;
  const { onOpen, onRefresh } = handlers || {};
  if (!onOpen || !onRefresh) return seenCount;

  // Only count tool calls that have a result — `tool_progress` events emit
  // partial entries while the tool is still running. We want the moment the
  // tool actually returns so the panel opens with real data.
  const completedDashboardCalls = toolCalls.filter((tc) => {
    if (!tc) return false;
    const name = tc.name || tc.tool_name || '';
    if (!DASHBOARD_TOOLS.has(name)) return false;
    return tc.results || tc.result || tc.status === 'ok' || tc.status === 'success';
  });

  if (completedDashboardCalls.length <= seenCount) return seenCount;

  onOpen();
  // Defer the refresh so the panel mounts + DashboardViewer can attach its
  // forwardRef before we call refresh(). Without this, the first refresh
  // after a stream-final 'done' event can race the mount and silently no-op.
  setTimeout(onRefresh, 0);
  return completedDashboardCalls.length;
}

export const __test__ = { DASHBOARD_TOOLS };
