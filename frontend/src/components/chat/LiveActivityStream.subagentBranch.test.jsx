/** 2026-08-25: Test for SubagentBranch grouping.
 *
 * The SubagentBranch helper groups child rows under their parent
 * subagent row. This is what makes the activity feed show a tree
 * (subagent at indent 0, child tools at indent 1) instead of a flat
 * list of duplicate rows.
 */
import { describe, it, expect } from 'vitest';
import { groupIntoBranches } from './LiveActivityStream';

describe('groupIntoBranches', () => {
  it('groups child tools under their parent subagent', () => {
    const rows = [
      { type: 'subagent_invoked', params: { subagent_id: 'sub_1', agent_label: 'ask_data_agent' } },
      { type: 'tool_call_started', params: { tool_label: 'ask_data_agent' }, parent_subagent_id: 'sub_1', indent: 1 },
      { type: 'tool_call_finished', params: { tool_label: 'ask_data_agent' }, parent_subagent_id: 'sub_1', indent: 1 },
      { type: 'subagent_returned', params: { agent_label: 'ask_data_agent' } },
    ];
    const out = groupIntoBranches(rows);
    expect(out).toHaveLength(1); // one branch
    expect(out[0].parent.type).toBe('subagent_invoked');
    // Children include 2 tool events + 1 subagent_returned (which closes
    // the delegation). subagent_returned is matched by target/agent_label
    // and attached as a child so the subagent lifecycle is in one branch.
    expect(out[0].children).toHaveLength(3);
    expect(out[0].children[0].type).toBe('tool_call_started');
    expect(out[0].children[2].type).toBe('subagent_returned');
  });

  it('rows without a parent become standalone (no children)', () => {
    const rows = [
      { type: 'phase_enter', params: {} },
      { type: 'tool_call_started', params: { tool_label: 'web_search' } },
    ];
    const out = groupIntoBranches(rows);
    expect(out).toHaveLength(2);
    expect(out[0].parent.type).toBe('phase_enter');
    expect(out[0].children).toEqual([]);
    expect(out[1].parent.type).toBe('tool_call_started');
    expect(out[1].children).toEqual([]);
  });

  it('preserves order of subagent rows and standalone rows', () => {
    const rows = [
      { type: 'subagent_invoked', params: { subagent_id: 'sub_1' } },
      { type: 'tool_call_started', parent_subagent_id: 'sub_1', indent: 1 },
      { type: 'phase_enter', params: {} }, // standalone, between branches
      { type: 'subagent_invoked', params: { subagent_id: 'sub_2' } },
      { type: 'tool_call_started', parent_subagent_id: 'sub_2', indent: 1 },
    ];
    const out = groupIntoBranches(rows);
    expect(out).toHaveLength(3); // branch1, standalone, branch2
    expect(out[0].parent.params.subagent_id).toBe('sub_1');
    expect(out[1].parent.type).toBe('phase_enter');
    expect(out[2].parent.params.subagent_id).toBe('sub_2');
  });
});
