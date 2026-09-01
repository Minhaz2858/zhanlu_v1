/** 2026-08-25: Test for collapsedChipSummary() helper.
 *
 * The CollapsedChip displays a single-line summary of a done event:
 * "✓ ask_data_agent · 131s · 12 rows". This helper extracts the
 * summary text from an event so the chip can render it.
 */
import { describe, it, expect } from 'vitest';
import { collapsedChipSummary } from './LiveActivityStream';

describe('collapsedChipSummary', () => {
  it('formats a tool_call_finished with duration', () => {
    const ev = {
      type: 'tool_call_finished',
      status: 'done',
      params: { tool_label: 'ask_data_agent', duration: 131 },
    };
    const out = collapsedChipSummary(ev);
    expect(out).toContain('ask_data_agent');
    expect(out).toContain('131s');
  });

  it('formats a subagent_returned with row count', () => {
    const ev = {
      type: 'subagent_returned',
      status: 'done',
      params: { agent_label: 'ask_data_agent', duration: 45, row_count: 12 },
    };
    const out = collapsedChipSummary(ev);
    expect(out).toContain('ask_data_agent');
    expect(out).toContain('45s');
    expect(out).toContain('12 rows');
  });

  it('marks failed events with a failure symbol', () => {
    const ev = {
      type: 'tool_call_failed',
      status: 'failed',
      params: { tool_label: 'ask_data_agent' },
    };
    const out = collapsedChipSummary(ev);
    // Should contain the tool name and indicate failure
    expect(out).toContain('ask_data_agent');
    expect(out).toMatch(/fail|✗/i);
  });

  it('falls back to a generic label for unknown event types', () => {
    const ev = { type: 'phase_enter', status: 'done', params: {} };
    const out = collapsedChipSummary(ev);
    expect(out.length).toBeGreaterThan(0);
  });

  it('handles missing duration gracefully', () => {
    const ev = {
      type: 'tool_call_finished',
      status: 'done',
      params: { tool_label: 'ask_data_agent' },
    };
    const out = collapsedChipSummary(ev);
    expect(out).toContain('ask_data_agent');
    expect(out).not.toContain('undefined');
  });
});
