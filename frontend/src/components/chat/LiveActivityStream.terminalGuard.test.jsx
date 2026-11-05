// Unified timeline acceptance (2026-11-01) — terminal-state consistency in
// LiveActivityStream's merge pipeline. Root cause of the "Running (live) vs
// Cancelled (panel)" screenshot: (a) LIFO stack pairing mispairs concurrent
// same-name tools and turns replayed starts into phantom duplicate rows;
// (b) residual running rows were relabeled 'interrupted' while the agent
// tree honestly reported 'cancelled'. Deterministic, no LLM.
import { describe, expect, it } from 'vitest';

import {
  mergeEvents,
  finalizeResidualRows,
} from './LiveActivityStream';

const started = (tool, ts, params = {}) => ({
  type: 'tool_call_started', ts, label_key: 'tool_call_started',
  params: { tool_name: tool, tool_label: tool, ...params },
});
const finished = (tool, ts, params = {}) => ({
  type: 'tool_call_finished', ts, label_key: 'tool_call_finished',
  params: { tool_name: tool, tool_label: tool, ...params },
});

describe('mergeEvents — stable operation identity', () => {
  it('a replayed started AFTER its finish does not create a phantom duplicate row', () => {
    const rows = mergeEvents([
      started('query_db', 't1'),
      finished('query_db', 't2'),
      started('query_db', 't1'), // SSE redelivery of the SAME operation
    ]);
    const ops = rows.filter((r) => r.params?.tool_name === 'query_db');
    expect(ops).toHaveLength(1);
    expect(ops[0].status).toBe('done');
  });

  it('concurrent same-name tools pair correctly (by tool identity, not LIFO order)', () => {
    const rows = mergeEvents([
      started('query_db', 't1'), // op A
      started('query_db', 't2'), // op B
      finished('query_db', 't3', { duration_ms: 100 }), // finishes B (later start)
      finished('query_db', 't4', { duration_ms: 900 }), // finishes A
    ]);
    const ops = rows.filter((r) => r.params?.tool_name === 'query_db');
    expect(ops).toHaveLength(2);
    expect(ops.every((r) => r.status === 'done')).toBe(true);
    expect(ops.map((r) => r.params.duration_ms).sort()).toEqual([100, 900]);
  });

  it('different-name concurrent tools are unaffected', () => {
    const rows = mergeEvents([
      started('query_db', 't1'),
      started('web_search', 't2'),
      finished('web_search', 't3'),
      finished('query_db', 't4'),
    ]);
    expect(rows.find((r) => r.params?.tool_name === 'query_db').status).toBe('done');
    expect(rows.find((r) => r.params?.tool_name === 'web_search').status).toBe('done');
  });

  it('a terminal event without a seen start still renders (no fabricated running)', () => {
    const rows = mergeEvents([finished('orphan_tool', 't1')]);
    expect(rows).toHaveLength(1);
    expect(rows[0].status).toBe('done');
  });
});

describe('finalizeResidualRows — cancellation distinguished from failure', () => {
  it('residual running rows become cancelled (same as the agent tree), not interrupted', () => {
    const rows = mergeEvents([started('query_db', 't1')]);
    const finalized = finalizeResidualRows(rows);
    expect(finalized[0].status).toBe('cancelled');
  });

  it('terminal rows are never overwritten', () => {
    const rows = mergeEvents([
      started('query_db', 't1'),
      finished('query_db', 't2'),
      started('web_search', 't3'),
    ]);
    const finalized = finalizeResidualRows(rows);
    expect(finalized.find((r) => r.params?.tool_name === 'query_db').status).toBe('done');
    expect(finalized.find((r) => r.params?.tool_name === 'web_search').status).toBe('cancelled');
  });

  it('failed rows stay failed (cancellation never masks a real failure)', () => {
    const rows = mergeEvents([
      started('query_db', 't1'),
      { type: 'tool_call_failed', ts: 't2', label_key: 'tool_call_failed', params: { tool_name: 'query_db' } },
    ]);
    const finalized = finalizeResidualRows(rows);
    expect(finalized[0].status).toBe('failed');
  });
});
