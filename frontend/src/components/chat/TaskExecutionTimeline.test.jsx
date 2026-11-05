/**
 * Dynamic orchestration timeline (2026-11-02) — event-driven derivation
 * tests. All scenarios derive from REAL backend event shapes (no LLM, no
 * simulation): plan_step_added/completed, plan_updated, agent_*,
 * finalize_done. Spec scenarios covered: direct execution, delegation,
 * parallel + sequential agents, peer collaboration, failure + retry,
 * supervisor replanning, dynamic add/supersede, partial results, duplicate
 * events, replay (persistence), turn isolation is enforced upstream (gen
 * guard) — here we pin that derivation is a pure function of the event
 * list (same events → same timeline, no cross-call state).
 */
import { describe, it, expect } from 'vitest';
import { derivePlanSteps, attributeAgentsToSteps, displayStepTitle } from './TaskExecutionTimeline.jsx';

const ev = (type, params = {}, ts = '2026-11-02T10:00:00Z', id) => ({ type, params, ts, id });

describe('derivePlanSteps — dynamic plan events', () => {
  it('direct execution: plan steps derive from plan_step_added/completed', () => {
    const steps = derivePlanSteps([
      ev('plan_step_added', { step_index: 1, task_id: 'task-a', title: 'Understand the bolt spec' }),
      ev('plan_step_added', { step_index: 2, task_id: 'task-b', title: 'Sketch and extrude' }),
      ev('plan_step_completed', { step_index: 1, task_id: 'task-a', title: 'Understand the bolt spec' }),
    ]);
    expect(steps).toHaveLength(2);
    expect(steps[0]).toMatchObject({ step_index: 1, status: 'done', task_id: 'task-a' });
    expect(steps[1]).toMatchObject({ step_index: 2, status: 'pending' });
  });

  it('plan lengths are fully dynamic — 2 steps and 8 steps both render', () => {
    const two = derivePlanSteps([
      ev('plan_step_added', { step_index: 1, title: 'Look at it' }),
      ev('plan_step_added', { step_index: 2, title: 'Answer' }),
    ]);
    expect(two).toHaveLength(2);
    const eight = derivePlanSteps(
      Array.from({ length: 8 }, (_, i) =>
        ev('plan_step_added', { step_index: i + 1, title: `Step ${i + 1}` })),
    );
    expect(eight).toHaveLength(8);
  });

  it('supervisor replanning: superseded task stays visible, completed preserved', () => {
    const steps = derivePlanSteps([
      ev('plan_step_added', { step_index: 1, task_id: 'task-a', title: 'Gather data' }),
      ev('plan_step_added', { step_index: 2, task_id: 'task-b', title: 'Old analysis' }),
      ev('plan_step_completed', { step_index: 1, task_id: 'task-a', title: 'Gather data' }),
      ev('plan_step_completed', { step_index: 2, task_id: 'task-b', title: 'Old analysis' }),
      // supervisor revises: old analysis superseded, new task added
      ev('plan_updated', { step_index: 2, task_id: 'task-b', title: 'Old analysis', status: 'superseded' }),
      ev('plan_step_added', { step_index: 3, task_id: 'task-c', title: 'Reconciled analysis' }),
    ]);
    expect(steps).toHaveLength(3);
    expect(steps[0]).toMatchObject({ status: 'done' });        // preserved
    expect(steps[1]).toMatchObject({ status: 'superseded' });  // visible, struck
    expect(steps[2]).toMatchObject({ status: 'pending', title: 'Reconciled analysis' });
  });

  it('replan re-opening a failed task: pending upgrade allowed, done never regresses', () => {
    const steps = derivePlanSteps([
      ev('plan_step_added', { step_index: 1, task_id: 'task-a', title: 'Query data' }),
      ev('plan_updated', { step_index: 1, task_id: 'task-a', title: 'Query data', status: 'failed' }),
      ev('plan_updated', { step_index: 1, task_id: 'task-a', title: 'Query data', status: 'pending' }),
      ev('plan_step_completed', { step_index: 1, task_id: 'task-a', title: 'Query data' }),
      ev('plan_updated', { step_index: 1, task_id: 'task-a', title: 'Query data', status: 'pending' }),
    ]);
    expect(steps[0].status).toBe('done'); // terminal is sticky after re-open cycle
  });

  it('duplicate events are idempotent (dedup/replay safety)', () => {
    const base = [
      ev('plan_step_added', { step_index: 1, task_id: 'task-a', title: 'Gather' }),
      ev('plan_step_completed', { step_index: 1, task_id: 'task-a', title: 'Gather' }),
    ];
    expect(derivePlanSteps([...base, ...base])).toEqual(derivePlanSteps(base));
  });

  it('replay from persisted events yields the same timeline (pure function)', () => {
    const events = [
      ev('plan_step_added', { step_index: 1, task_id: 'task-a', title: 'Gather' }),
      ev('agent_started', { agent_execution_id: 'ax1', agent_label: 'Sales Agent', plan_task_id: 'task-a' }),
      ev('plan_step_completed', { step_index: 1, task_id: 'task-a', title: 'Gather' }),
    ];
    expect(derivePlanSteps(events)).toEqual(derivePlanSteps([...events]));
  });
});

describe('attributeAgentsToSteps — agent → task nesting', () => {
  const steps = [
    { step_index: 1, task_id: 'task-a', status: 'done' },
    { step_index: 2, task_id: 'task-b', status: 'running' },
  ];

  it('explicit plan_task_id wins over timestamp heuristic (parallel agents)', () => {
    const events = [
      ev('plan_step_added', { step_index: 1, task_id: 'task-a', title: 'A' }),
      ev('plan_step_added', { step_index: 2, task_id: 'task-b', title: 'B' }),
      // two agents start at the SAME ts — parallel; explicit links decide
      ev('agent_started', { agent_execution_id: 'ax1', plan_task_id: 'task-b' }),
      ev('agent_started', { agent_execution_id: 'ax2', plan_task_id: 'task-b' }),
    ];
    const map = attributeAgentsToSteps(events, steps);
    expect(map.get('ax1')).toBe(2);
    expect(map.get('ax2')).toBe(2);
  });

  it('sequential agents fall back to timestamp attribution', () => {
    const events = [
      ev('plan_step_added', { step_index: 1, task_id: 'task-a', title: 'A' }, '2026-11-02T10:00:00Z'),
      ev('agent_started', { agent_execution_id: 'ax1' }, '2026-11-02T10:00:05Z'),
      ev('plan_step_completed', { step_index: 1, task_id: 'task-a', title: 'A' }, '2026-11-02T10:01:00Z'),
      ev('agent_started', { agent_execution_id: 'ax2' }, '2026-11-02T10:01:05Z'),
    ];
    const map = attributeAgentsToSteps(events, steps);
    expect(map.get('ax1')).toBe(1); // works step 1
    expect(map.get('ax2')).toBe(2); // works the next step
  });

  it('peer collaboration events do not fabricate agents', () => {
    const events = [
      ev('plan_step_added', { step_index: 1, task_id: 'task-a', title: 'A' }),
      ev('peer_collaboration_started', { agent_execution_id: 'ghost' }),
    ];
    const map = attributeAgentsToSteps(events, steps);
    expect(map.has('ghost')).toBe(false);
  });
});

describe('displayStepTitle — honest labels', () => {
  it('never renders redaction placeholders as titles', () => {
    expect(displayStepTitle({ step_index: 3, title: '[data]' })).toBe('Step 3');
    expect(displayStepTitle({ step_index: 3, title: 'Gather dealer data' })).toBe('Gather dealer data');
  });
});
