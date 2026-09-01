// Global, framework-agnostic store for in-flight automation executions.
//
// A "Run Now" click registers an execution here (via `register`), which makes
// the pinned AutomationExecutionStatusDrawer appear. The polling hook then
// keeps the entry's status/steps/skill_calls fresh until the execution
// reaches a terminal status, at which point it becomes "history".
//
// This lives outside React so it survives page navigation (the drawer is
// mounted at AppLayout and stays visible across route changes) and can be
// driven from a plain `runAutomationTask` helper without a component context.

// Canonical terminal statuses (mirrors chatPoll.TERMINAL_EXECUTION_STATUSES).
export const TERMINAL_EXECUTION_STATUSES = new Set([
  'completed',
  'succeeded',
  'failed',
  'cancelled',
  'timed_out',
  'skipped',
]);

// Max number of terminal executions we retain as history in the drawer.
export const MAX_HISTORY = 5;

let entries = new Map(); // executionId -> entry
const listeners = new Set();

function snapshot() {
  return Array.from(entries.values()).sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
}

function emit() {
  const snap = snapshot();
  listeners.forEach((fn) => {
    try {
      fn(snap);
    } catch {
      // A listener throwing must not break other subscribers.
    }
  });
}

function pruneHistory() {
  const terminal = snapshot().filter((e) => TERMINAL_EXECUTION_STATUSES.has(e.status));
  const excess = terminal.slice(MAX_HISTORY);
  excess.forEach((e) => entries.delete(e.executionId));
}

/** Register (or re-register) an execution. Returns the stored entry. */
export function registerExecution(entry) {
  const id = entry && entry.executionId;
  if (!id) return null;
  const prev = entries.get(id) || {};
  const next = {
    status: 'queued',
    taskId: null,
    taskName: null,
    sessionId: null,
    steps: [],
    skill_calls: [],
    ...prev,
    ...entry,
    executionId: id,
    updatedAt: Date.now(),
  };
  entries.set(id, next);
  emit();
  return next;
}

/** Merge a poll payload into an existing entry. No-op if the id is unknown. */
export function updateExecution(executionId, patch) {
  if (!entries.has(executionId)) return;
  const prev = entries.get(executionId);
  const next = { ...prev, ...(patch || {}), executionId, updatedAt: Date.now() };
  entries.set(executionId, next);
  // Keep a bounded history: drop the oldest terminal entries beyond the cap.
  pruneHistory();
  emit();
}

/** Remove an execution (e.g. user dismisses a history item). */
export function removeExecution(executionId) {
  if (entries.delete(executionId)) emit();
}

/** Subscribe to snapshot changes. Returns an unsubscribe function. */
export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** Current snapshot (sorted, most-recent first). */
export function getEntries() {
  return snapshot();
}

/** Split the snapshot into active (non-terminal) and terminal history. */
export function partitionEntries(snap) {
  const active = [];
  const history = [];
  for (const e of snap || []) {
    if (TERMINAL_EXECUTION_STATUSES.has(e.status)) history.push(e);
    else active.push(e);
  }
  return { active, history: history.slice(0, MAX_HISTORY) };
}
