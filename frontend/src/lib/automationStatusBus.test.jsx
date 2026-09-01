import { describe, it, expect, beforeEach } from 'vitest';
import {
  registerExecution,
  updateExecution,
  removeExecution,
  getEntries,
  partitionEntries,
  subscribe,
  MAX_HISTORY,
} from './automationStatusBus';
import { formatElapsed, formatSkillCall, stepText } from './skillExecutionFormatter';

describe('automationStatusBus', () => {
  beforeEach(() => {
    // Reset the module-level map between tests.
    for (const e of getEntries()) removeExecution(e.executionId);
  });

  it('registers an execution as queued and partitions it as active', () => {
    registerExecution({ executionId: 'e1', taskName: 'Daily sync' });
    const { active, history } = partitionEntries(getEntries());
    expect(active).toHaveLength(1);
    expect(active[0].status).toBe('queued');
    expect(active[0].taskName).toBe('Daily sync');
    expect(history).toHaveLength(0);
  });

  it('merges a poll payload into an existing entry', () => {
    registerExecution({ executionId: 'e1', taskName: 'T' });
    updateExecution('e1', { status: 'running', steps: [{ no: 1, text: 'x' }], skill_calls: [] });
    const e = getEntries()[0];
    expect(e.status).toBe('running');
    expect(e.steps).toHaveLength(1);
  });

  it('moves a terminal execution to history and prunes beyond MAX_HISTORY', () => {
    for (let i = 0; i < MAX_HISTORY + 2; i++) {
      registerExecution({ executionId: `e${i}`, taskName: `T${i}` });
      updateExecution(`e${i}`, { status: 'completed' });
    }
    const { active, history } = partitionEntries(getEntries());
    expect(active).toHaveLength(0);
    expect(history.length).toBeLessThanOrEqual(MAX_HISTORY);
    expect(history.length).toBe(MAX_HISTORY);
  });

  it('notifies subscribers on register and update', () => {
    const seen = [];
    const unsub = subscribe((snap) => seen.push(snap.length));
    registerExecution({ executionId: 'e1' });
    updateExecution('e1', { status: 'running' });
    expect(seen.length).toBe(2);
    unsub();
  });
});

describe('skillExecutionFormatter', () => {
  it('formats elapsed seconds', () => {
    expect(formatElapsed(null)).toBe('');
    expect(formatElapsed(45)).toBe('45s');
    expect(formatElapsed(83)).toBe('1m 23s');
    expect(formatElapsed(3600)).toBe('1h 0m');
  });

  it('formats skill calls by action', () => {
    expect(formatSkillCall({ skill_name: 'weekly_report', action: 'load' })).toBe('Load weekly_report');
    expect(formatSkillCall({ skill_name: 'erp_writeback', action: 'run' })).toBe('Run erp_writeback');
    expect(formatSkillCall(null)).toBe('');
  });

  it('extracts step text', () => {
    expect(stepText({ no: 1, text: 'Loading schema' })).toBe('Loading schema');
    expect(stepText({ title: 'Generating report' })).toBe('Generating report');
  });
});
