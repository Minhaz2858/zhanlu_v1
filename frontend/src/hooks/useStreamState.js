import { useState, useRef, useCallback } from 'react';

/**
 * Stream state machine: idle → sending → streaming → complete | error | aborted
 *
 * Returns the current state + actions to transition between states.
 * The abortController ref is exposed so ChatInput can wire a Stop button.
 */
export default function useStreamState() {
  const [state, setState] = useState('idle'); // idle | sending | streaming | complete | error | aborted
  const abortRef = useRef(null);

  const startSending = useCallback(() => {
    abortRef.current = new AbortController();
    setState('sending');
  }, []);

  const startStreaming = useCallback(() => {
    setState('streaming');
  }, []);

  const complete = useCallback(() => {
    abortRef.current = null;
    setState('complete');
  }, []);

  const error = useCallback(() => {
    abortRef.current = null;
    setState('error');
  }, []);

  /**
   * Abort the current stream (called by Stop button).
   * Returns true if an abort was actually performed.
   */
  const abort = useCallback(() => {
    if (abortRef.current && !abortRef.current.signal.aborted) {
      abortRef.current.abort();
    }
    abortRef.current = null;
    setState('aborted');
  }, []);

  const reset = useCallback(() => {
    if (abortRef.current && !abortRef.current.signal.aborted) {
      abortRef.current.abort();
    }
    abortRef.current = null;
    setState('idle');
  }, []);

  const isActive = state === 'sending' || state === 'streaming';

  return {
    state,
    isActive,
    abortRef,
    startSending,
    startStreaming,
    complete,
    error,
    abort,
    reset,
  };
}
