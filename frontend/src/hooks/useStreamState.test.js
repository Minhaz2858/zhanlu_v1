/**
 * useStreamState — finite-state machine tests.
 *
 * The FSM owns the Stop button visibility contract:
 *   isActive = state === 'sending' || state === 'streaming'
 *
 * Each terminal transition (complete / error / abort / reset) MUST flip
 * isActive back to false, otherwise ChatInput keeps the Stop button visible
 * after the agent has finished generating a response.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import useStreamState from '@/hooks/useStreamState';

describe('useStreamState — FSM transitions drive Stop button visibility', () => {
  beforeEach(() => {
    vi.useRealTimers();
  });

  it('starts in idle with isActive=false', () => {
    const { result } = renderHook(() => useStreamState());
    expect(result.current.state).toBe('idle');
    expect(result.current.isActive).toBe(false);
  });

  it('startSending flips isActive=true and creates an AbortController', () => {
    const { result } = renderHook(() => useStreamState());
    act(() => result.current.startSending());
    expect(result.current.state).toBe('sending');
    expect(result.current.isActive).toBe(true);
    expect(result.current.abortRef.current).toBeInstanceOf(AbortController);
    expect(result.current.abortRef.current.signal.aborted).toBe(false);
  });

  it('startStreaming after startSending keeps isActive=true', () => {
    const { result } = renderHook(() => useStreamState());
    act(() => result.current.startSending());
    act(() => result.current.startStreaming());
    expect(result.current.state).toBe('streaming');
    expect(result.current.isActive).toBe(true);
  });

  it('complete() after startStreaming flips isActive=false (the done-event contract)', () => {
    const { result } = renderHook(() => useStreamState());
    act(() => result.current.startSending());
    act(() => result.current.startStreaming());
    expect(result.current.isActive).toBe(true);

    act(() => result.current.complete());
    expect(result.current.state).toBe('complete');
    expect(result.current.isActive).toBe(false);
    expect(result.current.abortRef.current).toBeNull();
  });

  it('error() after startStreaming flips isActive=false', () => {
    const { result } = renderHook(() => useStreamState());
    act(() => result.current.startSending());
    act(() => result.current.startStreaming());
    expect(result.current.isActive).toBe(true);

    act(() => result.current.error());
    expect(result.current.state).toBe('error');
    expect(result.current.isActive).toBe(false);
  });

  it('abort() after startStreaming flips isActive=false AND signals the AbortController', () => {
    const { result } = renderHook(() => useStreamState());
    act(() => result.current.startSending());
    act(() => result.current.startStreaming());
    const ac = result.current.abortRef.current;
    expect(ac).toBeInstanceOf(AbortController);

    act(() => result.current.abort());
    expect(ac.signal.aborted).toBe(true);
    expect(result.current.state).toBe('aborted');
    expect(result.current.isActive).toBe(false);
  });

  it('reset() after startStreaming flips isActive=false and aborts', () => {
    const { result } = renderHook(() => useStreamState());
    act(() => result.current.startSending());
    act(() => result.current.startStreaming());
    const ac = result.current.abortRef.current;

    act(() => result.current.reset());
    expect(ac.signal.aborted).toBe(true);
    expect(result.current.state).toBe('idle');
    expect(result.current.isActive).toBe(false);
  });

  it('after a full cycle, a new startSending works correctly (idempotent reset)', () => {
    const { result } = renderHook(() => useStreamState());

    // First send
    act(() => result.current.startSending());
    act(() => result.current.startStreaming());
    act(() => result.current.complete());
    expect(result.current.isActive).toBe(false);

    // Second send — must produce a fresh AbortController and flip back to active
    act(() => result.current.startSending());
    expect(result.current.state).toBe('sending');
    expect(result.current.isActive).toBe(true);
    expect(result.current.abortRef.current).toBeInstanceOf(AbortController);
  });
});
