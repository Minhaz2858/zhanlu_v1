// Names of dashboard tool calls the agent can emit. Matches the tool
// registrations in `backend/app/services/tool_handlers/dashboard_tools.py`.
const DASHBOARD_TOOLS = new Set([
  'create_dashboard',
  'update_dashboard',
  'undo_dashboard_edit',
]);

export function dashboardArtifactFromToolCall(toolCall) {
  if (!toolCall) return null;
  const name = toolCall.name || toolCall.tool_name || '';
  if (!DASHBOARD_TOOLS.has(name)) return null;

  const result = toolCall.results || toolCall.result || {};
  const artifact = result.artifact || {};
  const dashboard = result.dashboard || {};
  const dashboardId = artifact.dashboard_id || dashboard.id || result.dashboard_id || result.id;
  if (!dashboardId) return null;

  const title = artifact.title || dashboard.name || result.title || 'Dashboard';
  return {
    type: 'dashboard',
    source: 'dashboard',
    id: dashboardId,
    artifact_id: dashboardId,
    dashboard_id: dashboardId,
    title,
    file_name: title,
    datasource_name: artifact.datasource_name || result.datasource_name,
    widget_count: artifact.widget_count ?? result.widget_count,
    live: true,
    has_preview: true,
  };
}

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
  const { onOpen, onArtifact, onRefresh } = handlers || {};
  if ((!onOpen && !onArtifact) || !onRefresh) return seenCount;

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

  const newCalls = completedDashboardCalls.slice(seenCount);
  if (onArtifact) {
    newCalls.map(dashboardArtifactFromToolCall).filter(Boolean).forEach((artifact) => onArtifact(artifact));
  } else {
    onOpen();
  }
  // Defer the refresh so the panel mounts + DashboardViewer can attach its
  // forwardRef before we call refresh(). Without this, the first refresh
  // after a stream-final 'done' event can race the mount and silently no-op.
  setTimeout(onRefresh, 0);
  return completedDashboardCalls.length;
}

export const __test__ = { DASHBOARD_TOOLS };
