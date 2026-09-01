/** 2026-08-25: Test for attachSubagentParent() helper.
 *
 * This helper pairs tool_call_started events with their parent
 * subagent_invoked events so the frontend can render them as a
 * single nested row (eliminates the duplicate-row visual noise).
 */
import { describe, it, expect } from 'vitest';
import { attachSubagentParent } from './LiveActivityStream';

describe('attachSubagentParent', () => {
  it('pairs tool_call_started with matching subagent_invoked by target', () => {
    const rows = [
      { type: 'subagent_invoked', params: { target: 'ask_data_agent', subagent_id: 'sub_1' } },
      { type: 'tool_call_started', params: { tool_label: 'ask_data_agent' } },
      { type: 'tool_call_finished', params: { tool_label: 'ask_data_agent' } },
      { type: 'subagent_returned', params: { agent_label: 'ask_data_agent' } },
    ];
    const out = attachSubagentParent(rows);
    const toolRow = out.find((r) => r.type === 'tool_call_started');
    expect(toolRow.parent_subagent_id).toBe('sub_1');
  });

  it('does not assign parent when no matching subagent is open', () => {
    const rows = [
      { type: 'tool_call_started', params: { tool_label: 'web_search' } },
    ];
    const out = attachSubagentParent(rows);
    expect(out[0].parent_subagent_id).toBeUndefined();
  });

  it('handles multiple subagents in sequence', () => {
    const rows = [
      { type: 'subagent_invoked', params: { target: 'ask_data_agent', subagent_id: 'sub_1' } },
      { type: 'tool_call_started', params: { tool_label: 'ask_data_agent' } },
      { type: 'subagent_returned', params: {} },
      { type: 'subagent_invoked', params: { target: 'ask_rag_research', subagent_id: 'sub_2' } },
      { type: 'tool_call_started', params: { tool_label: 'ask_rag_research' } },
    ];
    const out = attachSubagentParent(rows);
    const tools = out.filter((r) => r.type === 'tool_call_started');
    expect(tools[0].parent_subagent_id).toBe('sub_1');
    expect(tools[1].parent_subagent_id).toBe('sub_2');
  });

  it('pops subagent stack on subagent_returned', () => {
    const rows = [
      { type: 'subagent_invoked', params: { target: 'ask_data_agent', subagent_id: 'sub_1' } },
      { type: 'subagent_returned', params: { agent_label: 'ask_data_agent' } },
      { type: 'tool_call_started', params: { tool_label: 'ask_data_agent' } }, // no parent
    ];
    const out = attachSubagentParent(rows);
    expect(out[2].parent_subagent_id).toBeUndefined();
  });
});
