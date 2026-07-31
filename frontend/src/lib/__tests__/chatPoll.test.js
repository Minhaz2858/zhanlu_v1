import { describe, it, expect } from 'vitest';
// decideRefresh is a pure helper extracted from Chat.jsx's checkSession poll
// effect. It decides, given the previous and current /by-session payloads,
// whether to (a) reload the chat message list (catching async-written marker
// + agent response/failure) and (b) keep polling.
import { decideRefresh, TERMINAL_EXECUTION_STATUSES, isRunInFlight } from '../chatPoll';

describe('chatPoll.isRunInFlight', () => {
  it('true while the latest execution is non-terminal (queued/running)', () => {
    expect(isRunInFlight({ status: 'running' })).toBe(true);
    expect(isRunInFlight({ status: 'queued' })).toBe(true);
  });
  it('false once the execution reaches a terminal status', () => {
    expect(isRunInFlight({ status: 'completed' })).toBe(false);
    expect(isRunInFlight({ status: 'failed' })).toBe(false);
    expect(isRunInFlight({ status: 'succeeded' })).toBe(false);
    expect(isRunInFlight({ status: 'cancelled' })).toBe(false);
  });
  it('false for null/missing execution or status', () => {
    expect(isRunInFlight(null)).toBe(false);
    expect(isRunInFlight(undefined)).toBe(false);
    expect(isRunInFlight({})).toBe(false);
    expect(isRunInFlight({ status: null })).toBe(false);
  });
});

describe('chatPoll.decideRefresh', () => {
  it('first poll with a running execution: reload + keep polling', () => {
    const curr = { task: { id: 't1' }, executions: [{ id: 'e1', status: 'running' }] };
    const r = decideRefresh(null, curr);
    expect(r.reload).toBe(true);
    expect(r.keepPolling).toBe(true);
  });

  it('first poll with a queued execution: reload + keep polling', () => {
    const curr = { task: { id: 't1' }, executions: [{ id: 'e1', status: 'queued' }] };
    const r = decideRefresh(null, curr);
    expect(r.reload).toBe(true);
    expect(r.keepPolling).toBe(true);
  });

  it('transition running -> failed: reload once + one more poll to confirm stable', () => {
    const prev = { task: { id: 't1' }, executions: [{ id: 'e1', status: 'running' }] };
    const curr = { task: { id: 't1' }, executions: [{ id: 'e1', status: 'failed' }] };
    const r = decideRefresh(prev, curr);
    expect(r.reload).toBe(true); // status changed -> pick up the just-written failure msg
    expect(r.keepPolling).toBe(true); // one more poll to confirm stable
  });

  it('stable terminal (failed -> failed, same id): stop polling, no reload', () => {
    const prev = { task: { id: 't1' }, executions: [{ id: 'e1', status: 'failed' }] };
    const curr = { task: { id: 't1' }, executions: [{ id: 'e1', status: 'failed' }] };
    const r = decideRefresh(prev, curr);
    expect(r.reload).toBe(false);
    expect(r.keepPolling).toBe(false);
  });

  it('stable completed: stop polling, no reload', () => {
    const prev = { task: { id: 't1' }, executions: [{ id: 'e1', status: 'completed' }] };
    const curr = { task: { id: 't1' }, executions: [{ id: 'e1', status: 'completed' }] };
    const r = decideRefresh(prev, curr);
    expect(r.reload).toBe(false);
    expect(r.keepPolling).toBe(false);
  });

  it('new execution id appears (user clicked Run Now again): reload + keep polling', () => {
    const prev = { task: { id: 't1' }, executions: [{ id: 'e1', status: 'completed' }] };
    const curr = { task: { id: 't1' }, executions: [{ id: 'e2', status: 'running' }] };
    const r = decideRefresh(prev, curr);
    expect(r.reload).toBe(true);
    expect(r.keepPolling).toBe(true);
  });

  it('no automation task on this session: first-poll reload only, never poll', () => {
    const curr = { task: null, executions: [] };
    expect(decideRefresh(null, curr)).toEqual({ reload: true, keepPolling: false });
    expect(decideRefresh(curr, curr)).toEqual({ reload: false, keepPolling: false });
  });

  it('task exists but no executions yet (first run just queued, row not visible): keep polling', () => {
    const curr = { task: { id: 't1' }, executions: [] };
    const r = decideRefresh(null, curr);
    expect(r.reload).toBe(true);
    expect(r.keepPolling).toBe(true);
  });

  it('TERMINAL_EXECUTION_STATUSES includes the canonical terminal codes', () => {
    ['completed', 'succeeded', 'failed', 'cancelled', 'timed_out'].forEach((s) =>
      expect(TERMINAL_EXECUTION_STATUSES.has(s)).toBe(true),
    );
    // non-terminal must NOT be in the set
    expect(TERMINAL_EXECUTION_STATUSES.has('running')).toBe(false);
    expect(TERMINAL_EXECUTION_STATUSES.has('queued')).toBe(false);
  });
});
