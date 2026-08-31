// Names of dashboard tool calls the agent can emit. Matches the tool
// registrations in `backend/app/services/tool_handlers/dashboard_tools.py`.
const DASHBOARD_TOOLS = new Set([
  'create_dashboard',
  'update_dashboard',
  'undo_dashboard_edit',
  'create_fullstack_dashboard',
  'update_fullstack_dashboard',
]);

export function dashboardArtifactFromToolCall(toolCall) {
  if (!toolCall) return null;
  const name = toolCall.name || toolCall.tool_name || '';
  if (!DASHBOARD_TOOLS.has(name)) return null;

  const result = toolCall.results || toolCall.result || {};
  const artifact = result.artifact || {};
  const dashboard = result.dashboard || {};
  const dashboardApp = result.dashboard_app || {};

  // New full-stack pipeline: artifact {source:'dashboard_app', slug, title, app_url}.
  if (artifact.source === 'dashboard_app' || dashboardApp.slug || dashboardApp.app_url) {
    const slug = artifact.slug || dashboardApp.slug;
    if (!slug) return null;
    const title = artifact.title || dashboardApp.name || result.title || 'Dashboard';
    return {
      type: 'dashboard',
      source: 'dashboard_app',
      id: slug,
      artifact_id: slug,
      dashboard_id: slug,
      slug,
      title,
      file_name: title,
      datasource_name: artifact.datasource_name || result.datasource_name,
      app_url: artifact.app_url || dashboardApp.app_url || `/api/dashboards/apps/${slug}/`,
      metric_count: artifact.metric_count ?? dashboardApp.metric_count,
      // T5: AgentConversation id that built the app — lets the dock panel /
      // My Files offer "Open in chat" (deep-link ``/?conv=<id>``).
      chat_thread_id: artifact.chat_thread_id || dashboardApp.chat_thread_id || result.chat_thread_id || null,
      live: true,
      has_preview: true,
    };
  }

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
  // Notify the sidebar that a dashboard was just created/edited so the
  // dashboard-dedicated session icon (LayoutDashboard) appears immediately,
  // without waiting for a page reload. SessionList.jsx listens for this.
  try {
    window.dispatchEvent(new Event('zhanlu:dashboard-created'));
  } catch { /* non-browser env — best-effort */ }
  // Defer the refresh so the panel mounts + DashboardViewer can attach its
  // forwardRef before we call refresh(). Without this, the first refresh
  // after a stream-final 'done' event can race the mount and silently no-op.
  setTimeout(onRefresh, 0);
  return completedDashboardCalls.length;
}

export const __test__ = { DASHBOARD_TOOLS };
