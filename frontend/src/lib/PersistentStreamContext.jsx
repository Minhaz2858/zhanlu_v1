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
 *
 *   State:
 *     - isActive:            true while ANY stream is running (global)
 *     - activeSessionId:     sessionId of the currently running stream, or
 *                            null when idle. Lets consumers scope their
 *                            busy/loading UI to the streaming session so
 *                            other sessions stay interactive (concurrent
 *                            chat support).
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
 */

const PersistentStreamContext = createContext(null);

export function PersistentStreamProvider({ children }) {
  const genRef = useRef(0);
  const abortRef = useRef(null);
  const streamGenRef = useRef(-1);
  const [isActive, setIsActive] = useState(false);
  const [activeSessionId, setActiveSessionId] = useState(null);
  // Map<sessionId, partial assistant message>
  const pendingMessageRef = useRef(new Map());

  const startSending = useCallback((sessionId = null, gen = null) => {
    abortRef.current = new AbortController();
    if (gen !== null) streamGenRef.current = gen;
    setIsActive(true);
    setActiveSessionId(sessionId);
  }, []);

  const complete = useCallback(() => {
    abortRef.current = null;
    setIsActive(false);
    setActiveSessionId(null);
  }, []);

  /**
   * Abort the current stream (called by Stop button).
   */
  const abort = useCallback(() => {
    if (abortRef.current && !abortRef.current.signal.aborted) {
      abortRef.current.abort();
    }
    abortRef.current = null;
    setIsActive(false);
    setActiveSessionId(null);
  }, []);

  /**
   * Abort + reset to idle (used when a new generation takes over,
   * e.g. on session switch, clear, or delete).
   */
  const resetStream = useCallback(() => {
    if (abortRef.current && !abortRef.current.signal.aborted) {
      abortRef.current.abort();
    }
    abortRef.current = null;
    setIsActive(false);
    setActiveSessionId(null);
  }, []);

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
        startSending,
        complete,
        abort,
        resetStream,
        incrementGen,
        updatePendingMessage,
        getPendingMessage,
        clearPendingMessage,
        isActiveForSession,
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
