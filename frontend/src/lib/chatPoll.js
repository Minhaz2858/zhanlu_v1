// Pure helpers for the Chat.jsx "refresh messages after a Run Now / scheduled
// run" poll. Extracted so the decision logic is unit-testable without mounting
// the full Chat component (which wires base44 + authFetch + many children).
//
// Background: `trigger_now` fires the executor as a BACKGROUND asyncio task
// (services/automation_dispatcher.py) and returns immediately. The frontend
// navigates to `/chat?session=<sid>` and `loadMessages` runs ONCE on mount.
// The executor then writes the visible "Run Automation Task：" marker + the
// agent's response/failure ~20ms-ish later, AFTER the mount-time fetch. With
// no refresh, the user saw only the user bubble ("agent not responding").
//
// Fix: poll `GET /api/automations/by-session/{sid}` (already wired) on an
// interval. After each poll, `decideRefresh(prev, curr)` decides whether to
// reload the message list (catch async writes) and whether to keep polling.

// Canonical terminal execution statuses. The dispatcher writes one of these
// when an execution is done (success or terminal failure). `running`/`queued`
// are intentionally absent.
export const TERMINAL_EXECUTION_STATUSES = new Set([
  'completed',
  'succeeded',
  'failed',
  'cancelled',
  'timed_out',
]);

function _latest(payload) {
  if (!payload || !payload.task) return null;
  const ex = Array.isArray(payload.executions) ? payload.executions : [];
  return ex[0] || null;
}

// Decide whether to reload the chat message list and whether to keep polling,
// given the previous and current `/by-session` payloads.
//
// prev: previous { task, executions } payload, or null on the first poll.
// curr: current  { task, executions } payload.
//
// Returns { reload: boolean, keepPolling: boolean }.
//
// Rules (state machine keyed on the LATEST execution's id+status):
//   - No automation task on this session → first-poll reload only, never poll.
//   - First poll with a task → reload + keep polling (catches in-flight writes).
//   - Latest execution id changed (new run started) → reload + keep polling.
//   - Latest status changed (e.g. running → failed) → reload once (pick up the
//     just-written agent response/failure) + keep polling one more cycle to
//     confirm stability.
//   - Latest is non-terminal (queued/running/None) → reload + keep polling.
//   - Latest is terminal AND unchanged from the previous poll (stable
//     terminal) → stop polling, no reload.
export function decideRefresh(prev, curr) {
  const hasTask = !!(curr && curr.task);
  if (!hasTask) {
    // Normal chat (no automation). Reload only on the very first poll so an
    // initial mount still picks up existing messages; never poll afterwards.
    return { reload: !prev, keepPolling: false };
  }

  const cur = _latest(curr);
  const prv = _latest(prev);
  const curId = (cur && cur.id) || null;
  const curStatus = (cur && cur.status) || null;
  const prvId = (prv && prv.id) || null;
  const prvStatus = (prv && prv.status) || null;

  const curTerminal = !!curStatus && TERMINAL_EXECUTION_STATUSES.has(curStatus);
  const idChanged = !!curId && curId !== prvId;
  const statusChanged = curStatus !== prvStatus;
  const firstPoll = !prev;

  const reload = firstPoll || idChanged || statusChanged || !curTerminal;
  // Keep polling while the latest is non-terminal, or anything just changed
  // (one more poll confirms stability after a transition / new run).
  const keepPolling = !curTerminal || idChanged || statusChanged;
  return { reload, keepPolling };
}

// Whether an execution is still in flight (queued/running) — i.e. the
// automation run hasn't reached a terminal status yet. Drives the "agent is
// working" thinking indicator shown in the chat while a Run Now / scheduled
// run executes in the background (the executor is async, so without this the
// user sees the request bubble then silence until the result lands).
export function isRunInFlight(execution) {
  return !!(
    execution &&
    execution.status &&
    !TERMINAL_EXECUTION_STATUSES.has(execution.status)
  );
}
