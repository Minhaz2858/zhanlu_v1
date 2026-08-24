import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import {
  PersistentStreamProvider,
  usePersistentStream,
} from '@/lib/PersistentStreamContext';

/**
 * Regression tests for concurrent-chat support: the active stream state
 * must be scoped per session so that switching to a new chat session
 * while a previous session is still streaming leaves the new session's
 * input enabled (bug: "Input field disabled in new chat sessions while
 * a previous session is processing").
 */

function StreamProbe() {
  const stream = usePersistentStream();
  return (
    <div>
      <span data-testid="isActive">{String(stream.isActive)}</span>
      <span data-testid="activeSessionId">
        {stream.activeSessionId ?? 'null'}
      </span>
      <span data-testid="a-busy">
        {String(stream.isActiveForSession('session-A'))}
      </span>
      <span data-testid="b-busy">
        {String(stream.isActiveForSession('session-B'))}
      </span>
      <button onClick={() => stream.startSending('session-A', 1)}>
        startA
      </button>
      <button onClick={() => stream.startSending('session-B', 2)}>
        startB
      </button>
      <button onClick={() => stream.startSending(null, 3)}>startNoSid</button>
      <button onClick={() => stream.complete()}>complete</button>
      <button onClick={() => stream.abort()}>abort</button>
      <button onClick={() => stream.resetStream()}>reset</button>
    </div>
  );
}

function renderProbe() {
  render(
    <PersistentStreamProvider>
      <StreamProbe />
    </PersistentStreamProvider>
  );
}

describe('PersistentStreamContext session scoping', () => {
  it('enables a different session while one session is streaming', () => {
    renderProbe();
    fireEvent.click(screen.getByText('startA'));

    // Session A is busy…
    expect(screen.getByTestId('a-busy').textContent).toBe('true');
    expect(screen.getByTestId('isActive').textContent).toBe('true');
    expect(screen.getByTestId('activeSessionId').textContent).toBe(
      'session-A'
    );
    // …but session B is NOT — its input stays enabled.
    expect(screen.getByTestId('b-busy').textContent).toBe('false');
  });

  it('tracks the most recently started session', () => {
    renderProbe();
    fireEvent.click(screen.getByText('startA'));
    fireEvent.click(screen.getByText('startB'));

    expect(screen.getByTestId('a-busy').textContent).toBe('false');
    expect(screen.getByTestId('b-busy').textContent).toBe('true');
    expect(screen.getByTestId('activeSessionId').textContent).toBe(
      'session-B'
    );
  });

  it('complete() clears the active session for all sessions', () => {
    renderProbe();
    fireEvent.click(screen.getByText('startA'));
    fireEvent.click(screen.getByText('complete'));

    expect(screen.getByTestId('isActive').textContent).toBe('false');
    expect(screen.getByTestId('activeSessionId').textContent).toBe('null');
    expect(screen.getByTestId('a-busy').textContent).toBe('false');
    expect(screen.getByTestId('b-busy').textContent).toBe('false');
  });

  it('abort() and resetStream() also clear the active session', () => {
    renderProbe();
    fireEvent.click(screen.getByText('startB'));
    fireEvent.click(screen.getByText('abort'));
    expect(screen.getByTestId('activeSessionId').textContent).toBe('null');

    fireEvent.click(screen.getByText('startA'));
    fireEvent.click(screen.getByText('reset'));
    expect(screen.getByTestId('activeSessionId').textContent).toBe('null');
    expect(screen.getByTestId('isActive').textContent).toBe('false');
  });

  it('startSending without a sessionId keeps global isActive fallback', () => {
    renderProbe();
    fireEvent.click(screen.getByText('startNoSid'));

    // Legacy callers (no session) still see the global busy flag.
    expect(screen.getByTestId('isActive').textContent).toBe('true');
    expect(screen.getByTestId('activeSessionId').textContent).toBe('null');
    // isActiveForSession(null/undefined) falls back to global isActive.
    expect(screen.getByTestId('a-busy').textContent).toBe('false');
    expect(screen.getByTestId('b-busy').textContent).toBe('false');
  });
});
