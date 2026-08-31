/** 2026-08-27: PlanChecklist tests — the agent's live todo list.
 *
 * The backend emits plan_step_added (before the loop) and
 * plan_step_completed (as tool evidence finishes each step). The checklist
 * renders the plan the agent derives from the user's input and ticks steps
 * off as the agent follows it — the "understand → plan → follow → respond"
 * contract made visible.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import LiveActivityStream from './LiveActivityStream';

const RUNNING_EVENT = {
  type: 'tool_call_started',
  label_key: 'tool_call_started',
  params: {},
  status: 'running',
  ts: new Date().toISOString(),
};

function renderStream(props = {}) {
  return render(
    <LiveActivityStream events={[RUNNING_EVENT]} isStreaming {...props} />
  );
}

describe('PlanChecklist', () => {
  it('renders nothing when there are no plan steps', () => {
    renderStream({});
    expect(screen.queryByTestId('plan-checklist')).toBeNull();
  });

  it('shows all pending steps with the count', () => {
    renderStream({
      streamingPlanSteps: [
        { step_index: 1, title: 'Analyze the request', status: 'pending' },
        { step_index: 2, title: 'Inspect the bound data source', status: 'pending' },
      ],
    });
    expect(screen.getByTestId('plan-checklist')).toBeTruthy();
    expect(screen.getByTestId('plan-checklist').textContent).toContain('0/2');
    expect(screen.getByTestId('plan-step-1').textContent).toContain('Analyze the request');
    expect(screen.getByTestId('plan-step-2').textContent).toContain('Inspect the bound data source');
  });

  it('ticks completed steps off as the agent executes the plan', () => {
    renderStream({
      streamingPlanSteps: [
        { step_index: 1, title: 'Analyze the request', status: 'done' },
        { step_index: 2, title: 'Inspect the bound data source', status: 'pending' },
        { step_index: 3, title: 'Gather datasets', status: 'done' },
      ],
    });
    expect(screen.getByTestId('plan-checklist').textContent).toContain('2/3');
    // Done step carries the checkmark styling (line-through) and stays visible.
    expect(screen.getByTestId('plan-step-1').textContent).toContain('Analyze the request');
    expect(screen.getByTestId('plan-step-1').querySelector('svg')).toBeTruthy();
    // Pending step has no check icon.
    expect(screen.getByTestId('plan-step-2').querySelector('svg')).toBeNull();
  });

  it('orders steps by step_index even when events arrive out of order', () => {
    renderStream({
      streamingPlanSteps: [
        { step_index: 3, title: 'Gather datasets', status: 'pending' },
        { step_index: 1, title: 'Analyze the request', status: 'pending' },
        { step_index: 2, title: 'Inspect the bound data source', status: 'pending' },
      ],
    });
    const items = screen.getAllByTestId(/plan-step-/);
    expect(items[0].textContent).toContain('Analyze the request');
    expect(items[1].textContent).toContain('Inspect the bound data source');
    expect(items[2].textContent).toContain('Gather datasets');
  });
});
