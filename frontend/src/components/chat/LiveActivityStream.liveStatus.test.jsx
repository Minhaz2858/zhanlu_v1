/** 2026-08-27: LiveStatusLine tests — P0-3 one-line hybrid live status.
 *
 * While streaming: shows the raw reasoning text when reasoning is present,
 * falls back to a short tool-action label otherwise. Hidden when not live.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import LiveActivityStream from './LiveActivityStream';

function renderStream(props = {}) {
  return render(
    <LiveActivityStream
      events={[{ type: 'tool_call_started', label_key: 'tool_call_started', params: {}, status: 'running', ts: new Date().toISOString() }]}
      isStreaming
      {...props}
    />
  );
}

describe('LiveStatusLine', () => {
  it('renders nothing when not streaming', () => {
    render(
      <LiveActivityStream
        events={[{ type: 'tool_call_finished', label_key: 'tool_call_finished', params: {}, status: 'done', ts: new Date().toISOString() }]}
        isStreaming={false}
        streamingReasoning="thinking"
        streamingAction="execute_query"
      />
    );
    expect(screen.queryByTestId('live-status-line')).toBeNull();
  });

  it('shows raw reasoning text while the model is thinking', () => {
    renderStream({ streamingReasoning: 'I need to check the sales table first' });
    const line = screen.getByTestId('live-status-line');
    expect(line.textContent).toContain('Live');
    expect(line.textContent).toContain('I need to check the sales table first');
  });

  it('falls back to a tool-action label when tools fire', () => {
    renderStream({ streamingAction: 'execute_query' });
    const line = screen.getByTestId('live-status-line');
    expect(line.textContent).toContain('Live');
    expect(line.textContent).toContain('Running query');
  });

  it('prefers reasoning over the action label (hybrid)', () => {
    renderStream({ streamingReasoning: 'checking schema', streamingAction: 'execute_query' });
    const line = screen.getByTestId('live-status-line');
    expect(line.textContent).toContain('checking schema');
    expect(line.textContent).not.toContain('Running query');
  });

  it('renders nothing when both inputs are empty', () => {
    renderStream({});
    expect(screen.queryByTestId('live-status-line')).toBeNull();
  });
});
