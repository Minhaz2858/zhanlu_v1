// Chat UI & agent execution experience repair (2026-10-31) — Part C tests.
// Deterministic: pure functions over synthetic SSE event lists. No LLM.
import { describe, expect, it } from 'vitest';

import {
  buildTreeFromLiveEvents,
  summarizeNodes,
} from './AgentExecutionTree';
import { dedupeAndSortEvents, mergeEvents } from './LiveActivityStream';

const invoked = (id, name, extra = {}) => ({
  type: 'subagent_invoked',
  ts: '2026-10-31T10:00:00Z',
  params: { subagent_id: id, agent_name: name, agent_label: `Delegating to ${name}`, target: 'delegate_task', ...extra },
});
const returned = (id, extra = {}) => ({
  type: 'subagent_returned',
  ts: '2026-10-31T10:00:05Z',
  params: { agent_label: 'Delegating to sales_analyst', subagent_id: id, duration: 5, ...extra },
});
const unrelated = (type) => ({ type, ts: '2026-10-31T10:00:01Z', params: {} });

describe('buildTreeFromLiveEvents — Part C rules', () => {
  it('creates NO nodes (and no panel) for a conversational turn', () => {
    // phase/tool/plan events alone must NEVER become synthetic sub-agents.
    expect(buildTreeFromLiveEvents([unrelated('phase_enter'), unrelated('tool_call_started')])).toBeNull();
    expect(buildTreeFromLiveEvents([])).toBeNull();
  });

  it('renders real agent identity — never a generic "sub-agent"', () => {
    const tree = buildTreeFromLiveEvents([invoked('sub_1', 'sales_analyst'), returned('sub_1')]);
    expect(tree.roots).toHaveLength(1);
    expect(tree.roots[0].agent_name).toBe('sales_analyst');
    const blob = JSON.stringify(tree);
    expect(blob).not.toContain('sub-agent');
  });

  it('a completed delegation shows completed — never Running', () => {
    const tree = buildTreeFromLiveEvents([invoked('sub_1', 'sales_analyst'), returned('sub_1')]);
    expect(tree.roots[0].status).toBe('completed');
  });

  it('an unmatched invoked event at stream end is cancelled, NEVER Running', () => {
    const tree = buildTreeFromLiveEvents([invoked('sub_1', 'sales_analyst')]);
    expect(tree.roots[0].status).toBe('cancelled');
    expect(tree.roots[0].status).not.toBe('running');
  });

  it('outcome=interrupted is reported honestly (cancelled, not completed)', () => {
    const tree = buildTreeFromLiveEvents([invoked('sub_1', 'sales_analyst'), returned('sub_1', { outcome: 'interrupted' })]);
    expect(tree.roots[0].status).toBe('cancelled');
  });

  it('duplicate invoked events do not create duplicate nodes', () => {
    const tree = buildTreeFromLiveEvents([
      invoked('sub_1', 'sales_analyst'),
      invoked('sub_1', 'sales_analyst'), // SSE redelivery
      returned('sub_1'),
    ]);
    expect(tree.roots).toHaveLength(1);
  });

  it('out-of-order frames (returned before invoked in the array) still pair', () => {
    const tree = buildTreeFromLiveEvents([returned('sub_1'), invoked('sub_1', 'sales_analyst')]);
    expect(tree.roots).toHaveLength(1);
    expect(tree.roots[0].status).toBe('completed');
  });

  it('multiple specialists: node count matches real delegations', () => {
    const tree = buildTreeFromLiveEvents([
      invoked('sub_1', 'sales_analyst'),
      returned('sub_1'),
      invoked('sub_2', 'finance_agent'),
      returned('sub_2'),
      unrelated('phase_enter'), // noise — must not add a node
    ]);
    expect(tree.roots).toHaveLength(2);
    expect(tree.roots.map((r) => r.agent_name)).toEqual(['sales_analyst', 'finance_agent']);
  });

  it('summary is derived from the SAME nodes as the tree (rule 7)', () => {
    const tree = buildTreeFromLiveEvents([
      invoked('sub_1', 'sales_analyst'),
      returned('sub_1'),
      invoked('sub_2', 'finance_agent'), // never returned → cancelled
    ]);
    expect(tree.summary.total).toBe(2);
    expect(tree.summary.completed).toBe(1);
    expect(tree.summary.failed).toBe(1); // cancelled buckets with failed
    // Summary equals an independent recount of the rendered roots:
    expect(tree.summary).toEqual(summarizeNodes(tree.roots));
  });
});

describe('mergeEvents — interruption honesty', () => {
  it('outcome=interrupted marks the row interrupted, not done', () => {
    const rows = mergeEvents([invoked('sub_1', 'sales_analyst'), returned('sub_1', { outcome: 'interrupted' })]);
    // The paired row adopts the returned event's type (lifecycle merge).
    const sub = rows.find((r) => r.params?.subagent_id === 'sub_1');
    expect(sub.status).toBe('interrupted');
    // And a normal return is still done:
    const rowsOk = mergeEvents([invoked('sub_2', 'sales_analyst'), returned('sub_2')]);
    expect(rowsOk.find((r) => r.params?.subagent_id === 'sub_2').status).toBe('done');
  });

  it('duplicate frames with ids dedupe (last wins)', () => {
    const a = { ...invoked('sub_1', 'sales_analyst'), id: 'ev-1' };
    const rows = mergeEvents([a, { ...a, ts: '2026-10-31T10:00:02Z' }]);
    expect(rows.filter((r) => r.type === 'subagent_invoked')).toHaveLength(1);
  });

  it('dedupeAndSortEvents orders out-of-order frames by ts', () => {
    const sorted = dedupeAndSortEvents([
      { type: 'b', ts: '2026-10-31T10:00:05Z' },
      { type: 'a', ts: '2026-10-31T10:00:01Z' },
    ]);
    expect(sorted.map((e) => e.type)).toEqual(['a', 'b']);
  });
});
