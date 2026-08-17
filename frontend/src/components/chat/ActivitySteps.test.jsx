/**
 * ActivitySteps tests — collapse-on-finalize behavior.
 *
 * ActivitySteps is a leaf component that renders the inline numbered list
 * inside the assistant message bubble. It must:
 *   - Return null for empty/null/undefined steps (backward compat).
 *   - While any step is still `running`, always show the full list and
 *     hide the toggle button (so live progress is unobstructed).
 *   - Once every step has a terminal status (done/failed), auto-collapse
 *     to a one-line summary (e.g. "13 steps · 9 done · 4 failed").
 *   - Toggle the list when the user clicks the summary / chevron.
 *   - Stick to the user's manual expand choice on re-render.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within, fireEvent } from '@testing-library/react';

// Mock useLanguage so the language tests can override the returned lang
// without needing to mount the full LanguageProvider (which talks to base44).
// `vi.hoisted` shares the same vi.fn() instance between the mock factory
// (which is hoisted to the top) and the test bodies (which need to override
// its return value).
const useLanguageMock = vi.hoisted(() =>
  vi.fn(() => ({
    lang: 'zh',
    setLang: () => {},
    t: {
      chat: {
        phase: {
          init:     { verb: '正在理解', title: '正在读取你的请求' },
          goal:     { verb: '正在理解', title: '正在弄清你的需求' },
          context:  { verb: '正在定位', title: '正在收集相关上下文' },
          plan:     { verb: '正在编排', title: '正在制定执行方案' },
          gate:     { verb: '正在校验', title: '正在执行策略检查' },
          act:      { verb: '正在构建', title: '正在产出交付物' },
          observe:  { verb: '正在观察', title: '正在记录执行结果' },
          verify:   { verb: '正在核对', title: '正在检查输出结果' },
          finalize: { verb: '正在收尾', title: '正在整合最终回复' },
          done:     { verb: '已完成', title: '全部完成' },
          fail:     { verb: '已中断', title: '执行过程出现问题' },
          working:  { verb: '处理中', title: '正在处理' },
        },
      },
    },
  }))
);

vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: useLanguageMock,
}));

import ActivitySteps from './ActivitySteps';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const makeStep = (number, status, description) => ({
  number,
  status,
  description,
});

const STEP_RUNNING = (n) => makeStep(n, 'running', `Step ${n} running`);
const STEP_DONE = (n) => makeStep(n, 'done', `Step ${n} done`);
const STEP_FAILED = (n) => makeStep(n, 'failed', `Step ${n} failed`);

const SUMMARY_LABEL = 'activity-steps-toggle';
const LIST_LABEL = 'activity-steps-list';

const getSummary = () => screen.queryByTestId(SUMMARY_LABEL);
const getList = () => screen.queryByTestId(LIST_LABEL);

// All-done steps auto-collapse to a summary, hiding the rows. Several
// tests below want to expand the list first so they can introspect the
// rows / the reasoning panel. Centralizing this avoids repeating the
// "click if there is a toggle" pattern in every test.
const expandList = () => {
  const summary = getSummary();
  if (summary) fireEvent.click(summary);
};

beforeEach(() => {
  // Default language: zh. Tests that want English override this in their body.
  try { localStorage.removeItem('zhanlu_lang'); } catch {}
});

// ---------------------------------------------------------------------------
// Empty / null guards
// ---------------------------------------------------------------------------

describe('ActivitySteps — empty / null guards', () => {
  it('returns null when steps is undefined', () => {
    const { container } = render(<ActivitySteps />);
    expect(container.firstChild).toBeNull();
  });

  it('returns null when steps is null', () => {
    const { container } = render(<ActivitySteps steps={null} />);
    expect(container.firstChild).toBeNull();
  });

  it('returns null when steps is an empty array', () => {
    const { container } = render(<ActivitySteps steps={[]} />);
    expect(container.firstChild).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Streaming state: always show full list, no toggle
// ---------------------------------------------------------------------------

describe('ActivitySteps — streaming (any step running)', () => {
  it('renders all steps when at least one step is running', () => {
    const steps = [STEP_DONE(1), STEP_DONE(2), STEP_RUNNING(3)];
    render(<ActivitySteps steps={steps} />);

    const list = getList();
    expect(list).toBeInTheDocument();
    expect(getSummary()).toBeNull();

    // Verify each step's description is visible
    expect(within(list).getByText('Step 1 done')).toBeInTheDocument();
    expect(within(list).getByText('Step 2 done')).toBeInTheDocument();
    expect(within(list).getByText('Step 3 running')).toBeInTheDocument();
  });

  it('does not render the toggle button while any step is running', () => {
    const steps = [STEP_RUNNING(1), STEP_DONE(2)];
    render(<ActivitySteps steps={steps} />);
    expect(getSummary()).toBeNull();
    expect(getList()).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Finalized state: auto-collapse to summary
// ---------------------------------------------------------------------------

describe('ActivitySteps — finalized (all steps have terminal status)', () => {
  it('auto-collapses to summary when all steps are done', () => {
    const steps = [STEP_DONE(1), STEP_DONE(2), STEP_DONE(3)];
    render(<ActivitySteps steps={steps} />);

    expect(getSummary()).toBeInTheDocument();
    expect(getList()).toBeNull();

    // Summary copy: "3 steps · 3 done · 0 failed" (English fallback when lang=zh still uses step/done/failed words? — zh is the default)
    // We assert the numeric content is rendered. Default lang is 'zh'.
    expect(getSummary()).toHaveTextContent('3');
    expect(getSummary()).toHaveTextContent('3');
    expect(getSummary()).toHaveTextContent('0');
  });

  it('includes the failed count in the summary when some steps failed', () => {
    // 13 total: 9 done, 4 failed — matches the user's screenshot.
    const steps = [
      STEP_DONE(1), STEP_DONE(2), STEP_DONE(3), STEP_DONE(4), STEP_DONE(5),
      STEP_DONE(6), STEP_DONE(7), STEP_DONE(8), STEP_DONE(9),
      STEP_FAILED(10), STEP_FAILED(11), STEP_FAILED(12), STEP_FAILED(13),
    ];
    render(<ActivitySteps steps={steps} />);

    const summary = getSummary();
    expect(summary).toBeInTheDocument();
    expect(summary).toHaveTextContent('13');
    expect(summary).toHaveTextContent('9');
    expect(summary).toHaveTextContent('4');
    expect(getList()).toBeNull();
  });

  it('collapses immediately when the message is loaded already-finalized', () => {
    // Conversation reopen case: steps arrive in their final state with no
    // running items. The component must still auto-collapse.
    const steps = [STEP_DONE(1), STEP_FAILED(2)];
    render(<ActivitySteps steps={steps} />);
    expect(getSummary()).toBeInTheDocument();
    expect(getList()).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Toggle behavior
// ---------------------------------------------------------------------------

describe('ActivitySteps — toggle', () => {
  it('expands the list when the user clicks the summary', () => {
    const steps = [STEP_DONE(1), STEP_DONE(2)];
    render(<ActivitySteps steps={steps} />);

    expect(getList()).toBeNull();
    fireEvent.click(getSummary());

    const list = getList();
    expect(list).toBeInTheDocument();
    expect(within(list).getByText('Step 1 done')).toBeInTheDocument();
    expect(within(list).getByText('Step 2 done')).toBeInTheDocument();
    // Summary still present (it's the collapse control when expanded)
    expect(getSummary()).toBeInTheDocument();
  });

  it('re-collapses the list when the user clicks the toggle while expanded', () => {
    const steps = [STEP_DONE(1), STEP_DONE(2)];
    render(<ActivitySteps steps={steps} />);

    fireEvent.click(getSummary());
    expect(getList()).toBeInTheDocument();

    fireEvent.click(getSummary());
    expect(getList()).toBeNull();
  });

  it('sticky: manual expand survives a re-render with new step data', () => {
    // Start with two done steps, collapse auto-fires.
    const initial = [STEP_DONE(1), STEP_DONE(2)];
    const { rerender } = render(<ActivitySteps steps={initial} />);
    expect(getList()).toBeNull();

    // User clicks to expand.
    fireEvent.click(getSummary());
    expect(getList()).toBeInTheDocument();

    // New step data arrives (e.g. a tool-progress update). The list must
    // stay expanded — we do not yank the view closed.
    const updated = [...initial, STEP_DONE(3)];
    rerender(<ActivitySteps steps={updated} />);
    expect(getList()).toBeInTheDocument();
    expect(within(getList()).getByText('Step 3 done')).toBeInTheDocument();
  });

  it('aria-expanded reflects collapsed state on the toggle button', () => {
    const steps = [STEP_DONE(1), STEP_DONE(2)];
    render(<ActivitySteps steps={steps} />);

    const toggle = getSummary();
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveAttribute('aria-controls', 'activity-steps-list');

    fireEvent.click(toggle);
    expect(getSummary()).toHaveAttribute('aria-expanded', 'true');
  });
});

// ---------------------------------------------------------------------------
// Language copy
// ---------------------------------------------------------------------------

describe('ActivitySteps — language', () => {
  it('renders English copy when the user has English selected', () => {
    useLanguageMock.mockReturnValueOnce({ lang: 'en', setLang: () => {}, t: {} });
    const steps = [STEP_DONE(1), STEP_FAILED(2)];
    render(<ActivitySteps steps={steps} />);

    const summary = getSummary();
    expect(summary).toBeInTheDocument();
    expect(summary.textContent).toMatch(/steps/);
    expect(summary.textContent).toMatch(/done/);
    expect(summary.textContent).toMatch(/failed/);
  });

  it('renders Chinese copy by default', () => {
    // No localStorage override → lang defaults to 'zh'
    const steps = [STEP_DONE(1), STEP_FAILED(2)];
    render(<ActivitySteps steps={steps} />);

    const summary = getSummary();
    expect(summary).toBeInTheDocument();
    expect(summary.textContent).toMatch(/步/);
    expect(summary.textContent).toMatch(/成功/);
    expect(summary.textContent).toMatch(/失败/);
  });
});

// ---------------------------------------------------------------------------
// Defensive: ignore unknown step statuses
// ---------------------------------------------------------------------------

describe('ActivitySteps — defensive', () => {
  it('does not crash on unknown step status (falls back to running config)', () => {
    const steps = [{ number: 1, status: 'weird', description: 'unknown' }];
    // An "unknown" status is treated as "running" by STATUS_CONFIG fallback,
    // so the section must STAY expanded (i.e. not collapse to a summary).
    render(<ActivitySteps steps={steps} />);
    expect(getList()).toBeInTheDocument();
    expect(getSummary()).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Snapshot-style sanity: structure unchanged
// ---------------------------------------------------------------------------

describe('ActivitySteps — list structure preserved', () => {
  it('preserves failed line-through when expanded', () => {
    const steps = [STEP_DONE(1), STEP_FAILED(2)];
    render(<ActivitySteps steps={steps} />);

    // First click to expand from collapsed state
    fireEvent.click(getSummary());

    const failed = within(getList()).getByText('Step 2 failed');
    expect(failed.className).toMatch(/line-through/);

    const done = within(getList()).getByText('Step 1 done');
    expect(done.className).not.toMatch(/line-through/);
  });
});

// ---------------------------------------------------------------------------
// Phase headline (Claude-style "✳ Fathoming…" row)
// ---------------------------------------------------------------------------

describe('ActivitySteps — phase headline', () => {
  const PHASE_LABEL = 'activity-phase-headline';

  it('renders nothing when no phase is passed', () => {
    render(<ActivitySteps steps={[STEP_RUNNING(1)]} />);
    expect(screen.queryByTestId(PHASE_LABEL)).toBeNull();
  });

  it('renders the verb + title when a phase is passed (translated from state)', () => {
    // The backend SSE ships English verb/title, but the component must
    // IGNORE them and translate from phase.state using t.chat.phase.
    render(
      <ActivitySteps
        steps={[STEP_RUNNING(1)]}
        phase={{ state: 'goal', verb: 'Fathoming', title: 'Understanding what you need' }}
      />
    );
    const headline = screen.getByTestId(PHASE_LABEL);
    // Should show the zh translation from t.chat.phase.goal, NOT the English strings.
    expect(headline.textContent).toMatch(/正在理解/);
    expect(headline.textContent).toMatch(/正在弄清你的需求/);
    expect(headline.textContent).not.toMatch(/Fathoming/);
    expect(headline.textContent).not.toMatch(/Understanding what you need/);
  });

  it('renders the phase even when steps are finalized', () => {
    render(
      <ActivitySteps
        steps={[STEP_DONE(1)]}
        phase={{ state: 'done', verb: 'Done', title: 'All finished' }}
      />
    );
    expect(screen.getByTestId(PHASE_LABEL)).toBeInTheDocument();
    // Translated to zh: 已完成 · 全部完成
    expect(screen.getByTestId(PHASE_LABEL).textContent).toMatch(/已完成/);
    expect(screen.getByTestId(PHASE_LABEL).textContent).not.toMatch(/All finished/);
  });

  it('does not render a headline when state is missing', () => {
    // Without a state we have no key into t.chat.phase, so render nothing
    // (the backend's English-only verb/title would leak through otherwise).
    render(
      <ActivitySteps steps={[STEP_RUNNING(1)]} phase={{ verb: 'Working' }} />
    );
    expect(screen.queryByTestId(PHASE_LABEL)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Expandable step detail (Claude-style "click to see the bash" behavior)
// ---------------------------------------------------------------------------

describe('ActivitySteps — expandable step detail', () => {
  const expandable = (n) => `activity-step-expandable-${n}`;
  const detail = (n) => `activity-step-detail-${n}`;

  it('marks a step with command/output as expandable', () => {
    const steps = [{
      number: 1, status: 'done', description: 'Ran code',
      command: "print('hi')", output_preview: 'hi',
    }];
    render(<ActivitySteps steps={steps} />);
    expandList();
    expect(screen.getByTestId(expandable(1))).toBeInTheDocument();
  });

  it('does NOT mark a plain step (no detail) as expandable', () => {
    render(<ActivitySteps steps={[STEP_DONE(1)]} />);
    expandList();
    expect(screen.queryByTestId(expandable(1))).toBeNull();
  });

  it('hides the command/output block until clicked', () => {
    const steps = [{
      number: 1, status: 'done', description: 'Ran code',
      command: "print('hi')", output_preview: 'hi',
    }];
    render(<ActivitySteps steps={steps} />);
    expandList();
    expect(screen.queryByTestId(detail(1))).toBeNull();
  });

  it('reveals command + output preview on click', () => {
    const steps = [{
      number: 1, status: 'done', description: 'Ran code',
      command: "print('hi')", output_preview: 'result: 42',
    }];
    render(<ActivitySteps steps={steps} />);
    expandList();
    fireEvent.click(screen.getByTestId(expandable(1)));

    const block = screen.getByTestId(detail(1));
    expect(block.textContent).toMatch(/print\('hi'\)/);
    expect(block.textContent).toMatch(/result: 42/);
  });

  it('toggles back closed on a second click', () => {
    const steps = [{
      number: 1, status: 'done', description: 'Ran code',
      command: "print('hi')",
    }];
    render(<ActivitySteps steps={steps} />);
    expandList();
    const trigger = screen.getByTestId(expandable(1));

    fireEvent.click(trigger);
    expect(screen.getByTestId(detail(1))).toBeInTheDocument();

    fireEvent.click(trigger);
    expect(screen.queryByTestId(detail(1))).toBeNull();
  });

  it('keeps expandable rows independent (no shared expanded state)', () => {
    const steps = [
      { number: 1, status: 'done', description: 'A', command: 'cmd-a' },
      { number: 2, status: 'done', description: 'B', command: 'cmd-b' },
    ];
    render(<ActivitySteps steps={steps} />);
    expandList();

    // Expand only step 1.
    fireEvent.click(screen.getByTestId(expandable(1)));
    expect(screen.getByTestId(detail(1))).toBeInTheDocument();
    expect(screen.queryByTestId(detail(2))).toBeNull();

    // Step 2 stays independent.
    fireEvent.click(screen.getByTestId(expandable(2)));
    expect(screen.getByTestId(detail(2))).toBeInTheDocument();
    expect(screen.getByTestId(detail(1))).toBeInTheDocument(); // 1 still open
  });

  it('expands via keyboard (Enter key)', () => {
    const steps = [{
      number: 1, status: 'done', description: 'Ran code',
      command: "print('hi')",
    }];
    render(<ActivitySteps steps={steps} />);
    expandList();
    const trigger = screen.getByTestId(expandable(1));
    trigger.focus();
    fireEvent.keyDown(trigger, { key: 'Enter' });
    expect(screen.getByTestId(detail(1))).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Reasoning panel — sits inside the expanded step list so it shares the
// steps' collapse/expand behavior
// ---------------------------------------------------------------------------

describe('ActivitySteps — reasoning panel', () => {
  const REASONING_LABEL = 'activity-steps-reasoning';

  it('renders nothing for the reasoning panel when no reasoning is passed', () => {
    render(<ActivitySteps steps={[STEP_DONE(1), STEP_DONE(2)]} />);
    expandList();
    expect(screen.queryByTestId(REASONING_LABEL)).toBeNull();
  });

  it('renders the reasoning panel inside the step list when reasoning is passed', () => {
    const reasoning = 'the model thought about it for a while';
    render(
      <ActivitySteps
        steps={[STEP_DONE(1), STEP_DONE(2)]}
        reasoning={reasoning}
      />
    );
    expandList();

    const panel = screen.getByTestId(REASONING_LABEL);
    expect(panel).toBeInTheDocument();

    // The reasoning text is inside a <pre> inside the <details>
    expect(panel.textContent).toMatch(reasoning);
    // And the character count appears in the summary line
    expect(panel.textContent).toMatch(String(reasoning.length));
  });

  it('hides the reasoning panel when the steps auto-collapse to summary', () => {
    // Auto-collapses when every step has a terminal status and the user
    // has not clicked to expand. The reasoning panel lives inside the
    // step list region, so it must also be hidden — the user only sees
    // both by clicking the summary toggle.
    render(
      <ActivitySteps
        steps={[STEP_DONE(1), STEP_DONE(2)]}
        reasoning="hidden behind summary"
      />
    );

    expect(getList()).toBeNull();
    expect(screen.queryByTestId(REASONING_LABEL)).toBeNull();
  });

  it('reveals the reasoning panel after the user expands the summary', () => {
    render(
      <ActivitySteps
        steps={[STEP_DONE(1), STEP_DONE(2)]}
        reasoning="now visible after click"
      />
    );

    // Initially hidden.
    expect(screen.queryByTestId(REASONING_LABEL)).toBeNull();

    // Expand → panel appears.
    fireEvent.click(getSummary());
    expect(screen.getByTestId(REASONING_LABEL)).toBeInTheDocument();
  });

  it('renders the reasoning panel while steps are streaming (list always open)', () => {
    // Streaming state never collapses — the reasoning must be visible
    // throughout the turn so the user can watch the model think live.
    render(
      <ActivitySteps
        steps={[STEP_RUNNING(1), STEP_DONE(2)]}
        reasoning="thinking in real time"
      />
    );

    expect(getSummary()).toBeNull(); // no toggle while streaming
    expect(screen.getByTestId(REASONING_LABEL)).toBeInTheDocument();
  });

  it('ignores empty / whitespace-only reasoning strings', () => {
    render(
      <ActivitySteps
        steps={[STEP_RUNNING(1)]}
        reasoning="   "
      />
    );
    expect(screen.queryByTestId(REASONING_LABEL)).toBeNull();
  });
});

// Keep the vi import alive in case future tests add their own vi.fn / vi.mock
void vi;
