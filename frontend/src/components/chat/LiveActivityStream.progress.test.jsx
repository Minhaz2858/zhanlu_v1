/** 2026-08-25: Test for computeProgress() helper.
 *
 * The progress bar at the top of the activity feed derives its state
 * from the existing event list — no new SSE events needed.
 */
import { describe, it, expect } from 'vitest';
import { computeProgress } from './LiveActivityStream';

describe('computeProgress', () => {
  it('returns idle state when no events', () => {
    const out = computeProgress([]);
    expect(out.status).toBe('idle');
    expect(out.completedEvents).toBe(0);
    expect(out.totalEvents).toBe(0);
  });

  it('counts running vs completed events', () => {
    const events = [
      { type: 'phase_enter', status: 'done' },
      { type: 'tool_call_started', status: 'running' },
      { type: 'tool_call_started', status: 'running' },
      { type: 'tool_call_finished', status: 'done' },
    ];
    const out = computeProgress(events);
    expect(out.totalEvents).toBe(4);
    expect(out.completedEvents).toBe(2);
    expect(out.status).toBe('running');
  });

  it('estimates remaining time from completed durations', () => {
    const events = [
      { type: 'tool_call_finished', status: 'done', duration: 30 },
      { type: 'tool_call_finished', status: 'done', duration: 20 },
      { type: 'tool_call_started', status: 'running' },
      { type: 'tool_call_started', status: 'running' },
    ];
    const out = computeProgress(events);
    // Avg completed = 25s, 2 running → 50s
    expect(out.estimatedRemainingMs).toBe(50_000);
  });

  it('uses default 30s per event when no completed durations', () => {
    const events = [
      { type: 'tool_call_started', status: 'running' },
      { type: 'tool_call_started', status: 'running' },
    ];
    const out = computeProgress(events);
    // 2 running * 30s default = 60s
    expect(out.estimatedRemainingMs).toBe(60_000);
  });

  it('detects current phase from most recent phase_enter', () => {
    const events = [
      { type: 'phase_enter.fsm', label_key: 'Fathoming your request' },
      { type: 'phase_enter.data_query', label_key: 'Querying data sources' },
    ];
    const out = computeProgress(events);
    expect(out.currentPhase).toBe('Querying data sources');
  });

  it('returns completed status when nothing is running and at least one event is done', () => {
    const events = [
      { type: 'tool_call_finished', status: 'done' },
    ];
    const out = computeProgress(events);
    expect(out.status).toBe('completed');
  });

  it('returns failed status when any event is failed', () => {
    const events = [
      { type: 'tool_call_failed', status: 'failed' },
    ];
    const out = computeProgress(events);
    expect(out.status).toBe('failed');
  });
});
