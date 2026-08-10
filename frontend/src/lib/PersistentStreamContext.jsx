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
 *     - pendingMessageRef:   Map<sessionId, { content, toolCalls, artifacts, phase, loading }>
 *
 *   State:
 *     - isActive:            true while a stream is running
 *
 *   Actions:
 *     - startSending()       create a new AbortController
 *     - complete()           mark the stream as done
 *     - abort()              abort the current stream (Stop button)
 *     - incrementGen()       bump generation to invalidate in-flight streams
 *     - updatePendingMessage(sid, msg)   write accumulated content
 *     - getPendingMessage(sid)           read accumulated content
 *     - clearPendingMessage(sid)         clean up after merging
 */

const PersistentStreamContext = createContext(null);

export function PersistentStreamProvider({ children }) {
  const genRef = useRef(0);
  const abortRef = useRef(null);
  const [isActive, setIsActive] = useState(false);
  // Map<sessionId, partial assistant message>
  const pendingMessageRef = useRef(new Map());

  const startSending = useCallback(() => {
    abortRef.current = new AbortController();
    setIsActive(true);
  }, []);

  const complete = useCallback(() => {
    abortRef.current = null;
    setIsActive(false);
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

  return (
    <PersistentStreamContext.Provider
      value={{
        genRef,
        abortRef,
        isActive,
        startSending,
        complete,
        abort,
        resetStream,
        incrementGen,
        updatePendingMessage,
        getPendingMessage,
        clearPendingMessage,
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
