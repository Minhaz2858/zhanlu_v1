import React, { createContext, useContext, useState, useRef, useCallback } from 'react';

/**
 * PersistentStreamContext — manages streaming state that survives page
 * navigation.
 *
 * Why this exists:
 *   When the user starts a streaming response in Chat.jsx and then
 *   navigates to another page (Dashboard, My Space, etc.), the
 *   component unmounts and the for-await loop's setMessages calls
 *   become no-ops — the stream continues but the accumulated content
 *   is lost. When the user returns, the chat shows stale/missing
 *   content even though the backend has finished generating.
 *
 *   This context keeps the stream lifecycle and a message buffer alive
 *   at the AppLayout level (above React Router's <Outlet>), so the
 *   stream can complete in the background and the accumulated response
 *   is available when Chat remounts.
 *
 *   Navigation away from Chat does NOT abort the stream — only the
 *   explicit Stop button or a new user send does.
 *
 * Concurrent multi-session support (2026-08-31):
 *   In addition to the single "focused" stream (the one the user is
 *   currently looking at), this context tracks a `runStatusRef` registry
 *   so the SIDEBAR can show a per-session "running silently" indicator
 *   for *every* session that has a live run — including automations and
 *   background agent replies the user navigated away from. The focused
 *   stream still owns the global `abortRef`/`activeSessionId`; the
 *   registry is purely a status broadcaster that lets the sidebar render
 *   a spinner next to any busy session without disturbing the run.
 *
 *   See docs/plans/2026-08-31-concurrent-chat-sessions-design.md.
 *
 * Public surface (consumed via `usePersistentStream()`):
 *
 *   Refs (survive Chat unmount/remount):
 *     - genRef:              generation counter for stream invalidation
 *     - abortRef:            current AbortController
 *     - streamGenRef:        gen value of the LAST stream started via
 *                            startSending(). Lets a stale stream loop
 *                            decide whether it is still the owner of the
 *                            shared stream state (streamGenRef === gen)
 *                            or whether a newer stream superseded it
 *                            (streamGenRef > gen) and thus must not call
 *                            resetStream()/complete() and clobber the
 *                            newer stream's state.
 *     - pendingMessageRef:   Map<sessionId, { content, toolCalls, artifacts, phase, loading }>
 *     - runStatusRef:        Map<sessionId, 'running'|'queued'|'done'|'error'>
 *
 *   State:
 *     - isActive:            true while ANY stream is running (global)
 *     - activeSessionId:     sessionId of the currently running stream, or
 *                            null when idle. Lets consumers scope their
 *                            busy/loading UI to the streaming session so
 *                            other sessions stay interactive (concurrent
 *                            chat support).
 *     - runStatuses:         snapshot of `runStatusRef` for rendering
 *
 *   Actions:
 *     - startSending(sessionId, gen)  create a new AbortController
 *     - complete()           mark the stream as done
 *     - abort()              abort the current stream (Stop button)
 *     - incrementGen()       bump generation to invalidate in-flight streams
 *     - updatePendingMessage(sid, msg)   write accumulated content
 *     - getPendingMessage(sid)           read accumulated content
 *     - clearPendingMessage(sid)         clean up after merging
 *     - isActiveForSession(sessionId)    true iff the stream belongs to
 *                                        `sessionId`
 *     - setRunStatus(sid, status)        record a session's run status
 *                                        ('running'|'queued'|'done'|'error')
 *     - getRunStatus(sid)                read a session's current run status
 *     - canStartRun(sid)                 advisory per-user concurrency cap
 */

// Per-user advisory cap on simultaneously *running* (streaming) sessions.
// The backend is stateless per request and has no conversation lock, so N
// concurrent runs are safe to start; this only bounds how many the UI lets
// run at once to avoid DB-pool / socket exhaustion (see design doc §5.1).
export const MAX_CONCURRENT_RUNS = 3;

const PersistentStreamContext = createContext(null);

export function PersistentStreamProvider({ children }) {
  const genRef = useRef(0);
  const abortRef = useRef(null);
  const streamGenRef = useRef(-1);
  const [isActive, setIsActive] = useState(false);
  const [activeSessionId, setActiveSessionId] = useState(null);
  // Map<sessionId, partial assistant message>
  const pendingMessageRef = useRef(new Map());
  // Map<sessionId, 'running'|'queued'|'done'|'error'> — drives the sidebar
  // "running silently" indicator. Not the source of truth for the stream
  // lifecycle; purely a status broadcaster.
  const runStatusRef = useRef(new Map());
  const [runStatuses, setRunStatuses] = useState({});

  const _emitRunStatus = useCallback((next) => {
    setRunStatuses(next);
  }, []);

  const setRunStatus = useCallback((sid, status) => {
    if (!sid) return;
    runStatusRef.current.set(sid, status);
    _emitRunStatus(Object.fromEntries(runStatusRef.current.entries()));
  }, [_emitRunStatus]);

  const getRunStatus = useCallback((sid) => {
    if (!sid) return null;
    return runStatusRef.current.get(sid) || null;
  }, []);

  // Clear a session's run status, with a brief "done" flash so the sidebar
  // spinner disappears gracefully. Keeps the registry as the single source
  // of truth (no per-caller timers).
  const clearRunStatus = useCallback((sid) => {
    if (!sid) return;
    setRunStatus(sid, 'done');
    setTimeout(() => {
      if (runStatusRef.current.get(sid) === 'done') {
        runStatusRef.current.delete(sid);
        _emitRunStatus(Object.fromEntries(runStatusRef.current.entries()));
      }
    }, 2000);
  }, [setRunStatus, _emitRunStatus]);

  // Advisory: true if a brand-new run may start in `sid`. The focused
  // session (already streaming) is always allowed; otherwise we cap at
  // MAX_CONCURRENT_RUNS live runs. This does NOT hard-block — it lets the
  // caller show a "queued" hint. The actual single-stream enforcement is
  // the existing global abortRef/activeSessionId (switching focus never
  // aborts the previous run).
  const canStartRun = useCallback((sid) => {
    if (activeSessionId === sid) return true;
    let live = 0;
    for (const s of runStatusRef.current.values()) {
      if (s === 'running' || s === 'queued') live += 1;
    }
    // Count the global focused stream too (may not yet be in the registry).
    if (isActive && activeSessionId && activeSessionId !== sid) live += 1;
    return live < MAX_CONCURRENT_RUNS;
  }, [activeSessionId, isActive]);

  const startSending = useCallback((sessionId = null, gen = null) => {
    abortRef.current = new AbortController();
    if (gen !== null) streamGenRef.current = gen;
    setIsActive(true);
    setActiveSessionId(sessionId);
    if (sessionId) setRunStatus(sessionId, 'running');
  }, [setRunStatus]);

  const complete = useCallback(() => {
    if (activeSessionId) clearRunStatus(activeSessionId);
    abortRef.current = null;
    setIsActive(false);
    setActiveSessionId(null);
  }, [activeSessionId, clearRunStatus]);

  /**
   * Abort the current stream (called by Stop button).
   */
  const abort = useCallback(() => {
    if (abortRef.current && !abortRef.current.signal.aborted) {
      abortRef.current.abort();
    }
    if (activeSessionId) clearRunStatus(activeSessionId);
    abortRef.current = null;
    setIsActive(false);
    setActiveSessionId(null);
  }, [activeSessionId, clearRunStatus]);

  /**
   * Abort + reset to idle (used when a new generation takes over,
   * e.g. on session switch, clear, or delete).
   */
  const resetStream = useCallback(() => {
    if (abortRef.current && !abortRef.current.signal.aborted) {
      abortRef.current.abort();
    }
    if (activeSessionId) clearRunStatus(activeSessionId);
    abortRef.current = null;
    setIsActive(false);
    setActiveSessionId(null);
  }, [activeSessionId, clearRunStatus]);

  const incrementGen = useCallback(() => {
    genRef.current++;
  }, []);

  const updatePendingMessage = useCallback((sessionId, msg) => {
    pendingMessageRef.current.set(sessionId, msg);
  }, []);

  const getPendingMessage = useCallback((sessionId) => {
    return pendingMessageRef.current.get(sessionId) || null;
  }, []);

  const clearPendingMessage = useCallback((sessionId) => {
    pendingMessageRef.current.delete(sessionId);
  }, []);

  /**
   * True iff the currently running stream belongs to `sessionId`.
   * Falls back to the global `isActive` when sessionId is null/undefined
   * so legacy callers that don't track a session still behave correctly.
   */
  const isActiveForSession = useCallback(
    (sessionId) => {
      if (!sessionId) return isActive;
      return isActive && activeSessionId === sessionId;
    },
    [isActive, activeSessionId]
  );

  return (
    <PersistentStreamContext.Provider
      value={{
        genRef,
        abortRef,
        streamGenRef,
        isActive,
        activeSessionId,
        runStatuses,
        startSending,
        complete,
        abort,
        resetStream,
        incrementGen,
        updatePendingMessage,
        getPendingMessage,
        clearPendingMessage,
        isActiveForSession,
        setRunStatus,
        getRunStatus,
        clearRunStatus,
        canStartRun,
        MAX_CONCURRENT_RUNS,
      }}
    >
      {children}
    </PersistentStreamContext.Provider>
  );
}

/**
 * Hook to access the persistent stream context.
 * Must be used within a <PersistentStreamProvider>.
 */
export function usePersistentStream() {
  const ctx = useContext(PersistentStreamContext);
  if (!ctx) {
    throw new Error(
      'usePersistentStream must be used within a <PersistentStreamProvider>'
    );
  }
  return ctx;
}
