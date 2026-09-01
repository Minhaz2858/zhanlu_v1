/** 2026-08-25: Test for computeCollapseStates() helper.
 *
 * Auto-collapse: when a NEW event is running, older done/failed
 * events shrink to a chip. When the turn ends, all events expand
 * back so the user can review the full history.
 */
import { describe, it, expect } from 'vitest';
import { computeCollapseStates } from './LiveActivityStream';

describe('computeCollapseStates', () => {
  it('collapses done events when something newer is running', () => {
    const rows = [
      { type: 'tool_call_finished', status: 'done' },
      { type: 'tool_call_started', status: 'running' },
    ];
    const out = computeCollapseStates(rows);
    expect(out[0].collapsed).toBe(true);
    expect(out[1].collapsed).toBe(false);
  });

  it('expands all rows when nothing is running', () => {
    const rows = [
      { type: 'tool_call_finished', status: 'done' },
      { type: 'tool_call_finished', status: 'done' },
    ];
    const out = computeCollapseStates(rows);
    expect(out[0].collapsed).toBe(false);
    expect(out[1].collapsed).toBe(false);
  });

  it('keeps running events expanded even if older than other running', () => {
    const rows = [
      { type: 'tool_call_started', status: 'running' }, // running1
      { type: 'tool_call_started', status: 'running' }, // running2 (newest)
    ];
    const out = computeCollapseStates(rows);
    // Both running, neither collapsed
    expect(out[0].collapsed).toBe(false);
    expect(out[1].collapsed).toBe(false);
  });

  it('only collapses done events BEFORE the last running event', () => {
    const rows = [
      { type: 'tool_call_finished', status: 'done' },
      { type: 'tool_call_started', status: 'running' },
      { type: 'tool_call_finished', status: 'done' }, // after running, stays expanded
    ];
    const out = computeCollapseStates(rows);
    expect(out[0].collapsed).toBe(true);
    expect(out[1].collapsed).toBe(false);
    expect(out[2].collapsed).toBe(false);
  });

  it('does not collapse failed events that have an error message visible', () => {
    const rows = [
      { type: 'tool_call_failed', status: 'failed' },
    ];
    const out = computeCollapseStates(rows);
    // Single failed event, nothing running → not collapsed
    expect(out[0].collapsed).toBe(false);
  });

  it('returns a new array (does not mutate input)', () => {
    const rows = [{ type: 'tool_call_finished', status: 'done' }];
    const out = computeCollapseStates(rows);
    expect(out).not.toBe(rows);
    expect(rows[0].collapsed).toBeUndefined();
  });
});
