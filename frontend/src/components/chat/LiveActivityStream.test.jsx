import { describe, it, expect } from 'vitest';
import {
  dedupeAndSortEvents,
  planMetaLabel,
  recoveryEventLabel,
} from './LiveActivityStream';

describe('dedupeAndSortEvents (P2 Phase 4)', () => {
  it('dedupes by id, keeping the last occurrence (merged data)', () => {
    const events = [
      { id: '1', type: 'subagent_invoked', ts: '2026-10-31T12:00:01Z', params: { agent_label: 'A' } },
      { id: '1', type: 'subagent_returned', ts: '2026-10-31T12:00:05Z', params: { agent_label: 'A', duration: 4 } },
      { id: '2', type: 'tool_call_started', ts: '2026-10-31T12:00:02Z', params: { tool_label: 'q' } },
    ];
    const out = dedupeAndSortEvents(events);
    // id '1' collapsed to a single event (the later returned frame wins).
    expect(out).toHaveLength(2);
    const merged = out.find((e) => e.id === '1');
    expect(merged.type).toBe('subagent_returned');
    expect(merged.params.duration).toBe(4);
  });

  it('orders by ts with id tiebreak (no mis-pairing on reorder)', () => {
    const events = [
      { id: 'b', type: 'tool_call_finished', ts: '2026-10-31T12:00:03Z', params: { tool_label: 'q' } },
      { id: 'a', type: 'tool_call_started', ts: '2026-10-31T12:00:02Z', params: { tool_label: 'q' } },
      { id: 'c', type: 'subagent_invoked', ts: '2026-10-31T12:00:01Z', params: { agent_label: 'A' } },
    ];
    const out = dedupeAndSortEvents(events);
    expect(out.map((e) => e.id)).toEqual(['c', 'a', 'b']);
  });

  it('dedupes id-less identical frames so they do not double-render', () => {
    const events = [
      { type: 'phase_enter', ts: '2026-10-31T12:00:01Z', params: { label_key: 'thinking' } },
      { type: 'phase_enter', ts: '2026-10-31T12:00:01Z', params: { label_key: 'thinking' } },
    ];
    const out = dedupeAndSortEvents(events);
    expect(out).toHaveLength(1);
  });

  it('returns [] for null/non-array input and skips non-object entries', () => {
    expect(dedupeAndSortEvents(null)).toEqual([]);
    expect(dedupeAndSortEvents(undefined)).toEqual([]);
    expect(dedupeAndSortEvents([null, 1, { id: 'x', type: 't' }])).toHaveLength(1);
  });
});

describe('planMetaLabel (Phase 1.5 Gap 3)', () => {
  it('renders a compact structured summary — no free text, no chain-of-thought', () => {
    const label = planMetaLabel({
      intent_type: 'investigation',
      execution_strategy: 'multi_agent',
      risk_level: 'medium',
      acceptance_criteria: ['a', 'b'],
      required_approvals: false,
      objective_statement: 'SHOULD NOT APPEAR (free text stays out of the feed)',
    });
    expect(label).toBe('Plan · investigation · multi agent · medium risk · 2 criteria');
    expect(label).not.toContain('SHOULD NOT APPEAR');
  });

  it('flags required approvals and degrades gracefully when empty', () => {
    expect(planMetaLabel({ required_approvals: true })).toBe('Plan · approval required');
    expect(planMetaLabel({})).toBe('Plan');
    expect(planMetaLabel()).toBe('Plan');
  });

  it('supports Chinese summary text', () => {
    expect(planMetaLabel({ acceptance_criteria: ['x'], required_approvals: true }, false))
      .toBe('Plan · 1 条标准 · 需审批');
  });
});

describe('recoveryEventLabel (Phase 2.8)', () => {
  it('renders classification + recovery hint compactly', () => {
    const label = recoveryEventLabel(
      { params: { tool_label: 'execute_query', category: 'timeout', recovery: 'One bounded retry' } },
      true,
    );
    expect(label).toContain('execute_query');
    expect(label).toContain('timeout');
    expect(label).toContain('One bounded retry');
  });

  it('renders decision events with the structured next action', () => {
    const label = recoveryEventLabel(
      { params: { decision: 'delegate_specialist', action: 'Delegate targeted evidence gathering' } },
      true,
    );
    expect(label).toContain('delegate specialist');
    expect(label).toContain('Delegate targeted evidence gathering');
  });

  it('never renders chain-of-thought fields even if present', () => {
    const label = recoveryEventLabel(
      { params: { decision: 'retry', action: 'retry once', reasoning: 'PRIVATE CHAIN OF THOUGHT' } },
      true,
    );
    expect(label).not.toContain('PRIVATE CHAIN OF THOUGHT');
  });

  it('degrades gracefully on empty params', () => {
    expect(recoveryEventLabel({ params: {} }, true)).toBe('');
    expect(recoveryEventLabel(undefined, true)).toBe('');
  });
});
