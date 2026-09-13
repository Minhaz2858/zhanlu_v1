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

  it('rebuilds the checklist from persisted plan_step events after reload', () => {
    // 2026-08-31: the backend now persists plan_step_added / plan_step_completed
    // as typed live events. A reloaded message (no streamingPlanSteps) must show
    // the same plan the user saw live — not degrade to a generic event chip.
    renderStream({
      streamingPlanSteps: undefined,
      events: [
        { type: 'phase_enter', label_key: 'phase_enter.plan', params: {}, ts: '2026-08-31T00:00:00Z' },
        { type: 'plan_step_added', label_key: 'plan_step_added', params: { step_index: 1, title: 'Analyze the request' }, ts: '2026-08-31T00:00:01Z' },
        { type: 'plan_step_added', label_key: 'plan_step_added', params: { step_index: 2, title: 'Inspect the bound data source' }, ts: '2026-08-31T00:00:02Z' },
        { type: 'plan_step_completed', label_key: 'plan_step_completed', params: { step_index: 1 }, ts: '2026-08-31T00:00:03Z' },
        { type: 'verify_passed', label_key: 'verify_passed', params: {}, ts: '2026-08-31T00:00:04Z' },
        { type: 'finalize_started', label_key: 'finalize_started', params: {}, ts: '2026-08-31T00:00:05Z' },
        { type: 'phase_enter', label_key: 'phase_enter.finalize', params: {}, ts: '2026-08-31T00:00:06Z' },
      ],
    });
    const checklist = screen.getByTestId('plan-checklist');
    expect(checklist.textContent).toContain('1/2');
    expect(screen.getByTestId('plan-step-1').textContent).toContain('Analyze the request');
    expect(screen.getByTestId('plan-step-1').querySelector('svg')).toBeTruthy(); // done tick
    expect(screen.getByTestId('plan-step-2').textContent).toContain('Inspect the bound data source');
    expect(screen.getByTestId('plan-step-2').querySelector('svg')).toBeNull(); // pending
  });

  it('does not render plan_step events as generic feed rows', () => {
    // Plan checklist metadata must not double-render inside the event list.
    renderStream({
      streamingPlanSteps: undefined,
      events: [
        { type: 'plan_step_added', label_key: 'plan_step_added', params: { step_index: 1, title: 'Analyze' }, ts: '2026-08-31T00:00:00Z' },
        { type: 'verify_passed', label_key: 'verify_passed', params: {}, ts: '2026-08-31T00:00:01Z' },
        { type: 'finalize_started', label_key: 'finalize_started', params: {}, ts: '2026-08-31T00:00:02Z' },
        { type: 'phase_enter', label_key: 'phase_enter.finalize', params: {}, ts: '2026-08-31T00:00:03Z' },
      ],
    });
    // The checklist renders the single step…
    expect(screen.getByTestId('plan-step-1').textContent).toContain('Analyze');
    // …and the feed shows only the non-plan rows (verify + finalize = 2),
    // never raw "plan_step_added" labels.
    const list = screen.getByTestId('live-activity-list');
    expect(list.textContent).not.toContain('plan_step_added');
    expect(list.textContent).not.toContain('plan_step_completed');
  });
});
