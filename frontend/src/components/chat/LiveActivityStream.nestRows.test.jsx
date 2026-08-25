/** 2026-08-25: Test for nestRows() helper.
 *
 * Given rows with parent_subagent_id set (from attachSubagentParent),
 * nestRows() computes a visual indent level for each row.
 *   - 0 = top-level (subagent or no parent)
 *   - 1 = child of a subagent
 */
import { describe, it, expect } from 'vitest';
import { nestRows } from './LiveActivityStream';

describe('nestRows', () => {
  it('top-level rows get indent 0', () => {
    const rows = [
      { type: 'phase_enter', params: {} },
      { type: 'tool_call_started', params: { tool_label: 'web_search' } },
    ];
    const out = nestRows(rows);
    expect(out[0].indent).toBe(0);
    expect(out[1].indent).toBe(0);
  });

  it('tool rows with parent_subagent_id get indent 1', () => {
    const rows = [
      { type: 'subagent_invoked', params: { subagent_id: 'sub_1' } },
      { type: 'tool_call_started', params: { tool_label: 'ask_data_agent' }, parent_subagent_id: 'sub_1' },
      { type: 'tool_call_finished', params: { tool_label: 'ask_data_agent' }, parent_subagent_id: 'sub_1' },
    ];
    const out = nestRows(rows);
    expect(out[0].indent).toBe(0);
    expect(out[1].indent).toBe(1);
    expect(out[2].indent).toBe(1);
  });

  it('returns a new array (does not mutate input)', () => {
    const rows = [{ type: 'phase_enter', params: {} }];
    const out = nestRows(rows);
    expect(out).not.toBe(rows);
    expect(rows[0].indent).toBeUndefined(); // input unchanged
    expect(out[0].indent).toBe(0);
  });
});
