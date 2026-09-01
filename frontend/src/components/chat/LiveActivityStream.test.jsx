/**
 * LiveActivityStream tests — typed live-activity feed.
 *
 * The stream is a leaf component rendered inside the assistant message
 * bubble. It must:
 *   - Return null when there are no events (chitchat turns render no box).
 *   - Render every event type with a human label resolved from the
 *     localized template map (zh + en) — never raw label keys.
 *   - Merge tool_call_started/finished pairs into a single row.
 *   - Be expanded while the stream is live; auto-collapse to a one-line
 *     summary ~1s after the stream finishes; honor manual expand.
 *   - Render the collapsible "Thinking" sub-section only when reasoning
 *     is present, collapsed by default.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, within, act } from '@testing-library/react';

const zhLiveEvent = {
  summary: '{n} 个事件 · {m} 已完成 · 共{s}秒',
  expand: '展开', collapse: '收起', thinking: '思考', running: '进行中',
  phase_enter: {
    init: '正在准备执行', goal: '正在理解你的请求', act: '正在构建交付物',
    verify: '正在校验结果', finalize: '正在生成最终答复', done: '全部完成',
  },
  plan_preview: '计划已生成 · 共{n}步',
  tool_call_started: '正在执行{tool_label}',
  tool_call_finished: '{tool_label}完成 · {row_count}行 · {duration}秒',
  tool_call_finished_short: '{tool_label}完成',
  tool_call_failed: '{tool_label}执行失败',
  artifact_progress: '正在构建{artifact_type} · {current}/{total}',
  verify_passed: '校验通过',
  retry: '正在修正{target}',
  finalize_started: '正在生成最终答复',
  subagent_invoked: '委托给{agent_label}',
  subagent_returned: '{agent_label}已返回 · {duration}秒',
  data_offer: '{row_count}行的样本',
  plan_summary: '计划已就绪 · 共{n}步',
};

const zhPhase = {
  init: { verb: '正在理解', title: '正在读取你的请求' },
  goal: { verb: '正在理解', title: '正在弄清你的需求' },
  plan: { verb: '正在编排', title: '正在制定执行方案' },
  act: { verb: '正在构建', title: '正在产出交付物' },
};

const enLiveEvent = {
  summary: '{n} events · {m} completed · {s}s',
  expand: 'Expand', collapse: 'Collapse', thinking: 'Thinking', running: 'In progress',
  phase_enter: {
    init: 'Preparing the run', goal: 'Fathoming your request', act: 'Building your deliverable',
    verify: 'Checking the outputs', finalize: 'Wrapping everything up', done: 'All finished',
  },
  plan_preview: 'Plan ready · {n} steps',
  tool_call_started: 'Running {tool_label}',
  tool_call_finished: '{tool_label} completed · {row_count} rows · {duration}s',
  tool_call_finished_short: '{tool_label} completed',
  tool_call_failed: '{tool_label} failed',
  artifact_progress: 'Building {artifact_type} · {current} of {total}',
  verify_passed: 'Verification passed',
  retry: 'Correcting {target}',
  finalize_started: 'Finalizing your answer',
  subagent_invoked: 'Delegating to {agent_label}',
  subagent_returned: '{agent_label} returned · {duration}s',
  data_offer: 'Sample of {row_count} rows',
  plan_summary: 'Plan ready · {n} steps',
};

const enPhase = {
  init: { verb: 'Fathoming', title: 'Reading your request' },
  goal: { verb: 'Fathoming', title: 'Understanding your request' },
  plan: { verb: 'Orchestrating', title: 'Laying out the plan' },
  act: { verb: 'Building', title: 'Producing the deliverable' },
};

const useLanguageMock = vi.hoisted(() =>
  vi.fn(() => ({
    lang: 'zh',
    setLang: () => {},
    t: { chat: { phase: zhPhase, liveEvent: zhLiveEvent } },
  }))
);

vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: useLanguageMock,
}));

import LiveActivityStream from './LiveActivityStream';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const ev = (type, label_key, params = {}, extra = {}) => ({
  type, label_key, params, ts: '2026-08-22T09:00:00.000Z', ...extra,
});

const TOOL_ROW = () => [
  ev('tool_call_started', 'tool_call_started', { tool_label: 'Querying data' }),
  ev('tool_call_finished', 'tool_call_finished',
    { tool_label: 'Querying data', row_count: 847, duration: 1.2 }),
];

const FULL_FEED = () => [
  ev('phase_enter', 'phase_enter.goal'),
  ev('tool_call_started', 'tool_call_started', { tool_label: 'Querying data' }),
  ev('tool_call_finished', 'tool_call_finished',
    { tool_label: 'Querying data', row_count: 847, duration: 1.2 }),
  ev('verify_passed', 'verify_passed'),
  ev('finalize_started', 'finalize_started'),
];

const getStream = () => screen.queryByTestId('live-activity-stream');
const getList = () => screen.queryByTestId('live-activity-list');
const getSummary = () => screen.queryByTestId('live-activity-summary');
const getHeadline = () => screen.queryByTestId('live-activity-headline');

beforeEach(() => {
  useLanguageMock.mockReturnValue({
    lang: 'zh',
    setLang: () => {},
    t: { chat: { phase: zhPhase, liveEvent: zhLiveEvent } },
  });
});

afterEach(() => {
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// Empty / null guards — chitchat turns render no box
// ---------------------------------------------------------------------------

describe('LiveActivityStream — empty state', () => {
  it('returns null when events is undefined', () => {
    const { container } = render(<LiveActivityStream />);
    expect(container.firstChild).toBeNull();
  });

  it('returns null when events is an empty array and stream has finished', () => {
    const { container } = render(<LiveActivityStream events={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders a rich placeholder while streaming with no events yet', () => {
    render(<LiveActivityStream events={[]} isStreaming />);
    expect(getStream()).toBeInTheDocument();
    expect(getHeadline()).toHaveTextContent('正在理解');
    expect(getHeadline()).toHaveTextContent('正在读取你的请求');
    expect(screen.getByText('计划')).toBeInTheDocument();
    expect(screen.getByText('正在准备计划…')).toBeInTheDocument();
  });

  it('renders streamed plan steps in the placeholder before any events arrive', () => {
    const steps = [
      { step_index: 0, title: 'Analyze request', status: 'pending' },
      { step_index: 1, title: 'Search the web', status: 'done' },
    ];
    render(
      <LiveActivityStream
        events={[]}
        isStreaming
        streamingPlanSteps={steps}
      />
    );
    expect(getStream()).toBeInTheDocument();
    expect(screen.getByTestId('plan-checklist')).toHaveTextContent('Analyze request');
    expect(screen.getByTestId('plan-checklist')).toHaveTextContent('Search the web');
  });

  it('renders placeholder in English when lang is en', () => {
    useLanguageMock.mockReturnValue({
      lang: 'en',
      setLang: () => {},
      t: { chat: { phase: enPhase, liveEvent: enLiveEvent } },
    });
    render(<LiveActivityStream events={[]} isStreaming />);
    expect(getHeadline()).toHaveTextContent('Fathoming');
    expect(getHeadline()).toHaveTextContent('Reading your request');
    expect(screen.getByText('Plan')).toBeInTheDocument();
    expect(screen.getByText('Preparing plan…')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Event rendering — every type resolves to a human label
// ---------------------------------------------------------------------------

describe('LiveActivityStream — event rendering', () => {
  it('renders all event types with localized labels (zh)', () => {
    const events = [
      ev('phase_enter', 'phase_enter.goal'),
      ev('plan_preview', 'plan_preview', { n: 3 }),
      ev('tool_call_started', 'tool_call_started', { tool_label: 'Querying data' }),
      ev('tool_call_finished', 'tool_call_finished',
        { tool_label: 'Querying data', row_count: 847, duration: 1.2 }),
      ev('tool_call_failed', 'tool_call_failed', { tool_label: 'create_dashboard' }),
      ev('artifact_progress', 'artifact_progress', { artifact_type: 'dashboard', current: 2, total: 4 }),
      ev('verify_passed', 'verify_passed'),
      ev('retry', 'retry', { target: 'query' }),
      ev('finalize_started', 'finalize_started'),
    ];
    render(<LiveActivityStream events={events} isStreaming />);

    const list = getList();
    expect(list).toBeInTheDocument();
    const text = list.textContent;
    expect(text).toContain('正在理解你的请求');
    expect(text).toContain('计划已生成 · 共3步');
    // The started/finished pair merges → only the finished label survives.
    expect(text).not.toContain('正在执行Querying data');
    expect(text).toContain('Querying data完成 · 847行 · 1.2秒');
    expect(text).toContain('create_dashboard执行失败');
    expect(text).toContain('正在构建dashboard · 2/4');
    expect(text).toContain('校验通过');
    expect(text).toContain('正在修正query');
    expect(text).toContain('正在生成最终答复');
    // No raw label_key strings leak into the feed.
    expect(text).not.toContain('tool_call_started');
    expect(text).not.toContain('verify_passed');
  });

  it('renders in English when lang is en', () => {
    useLanguageMock.mockReturnValue({
      lang: 'en',
      setLang: () => {},
      t: { chat: { phase: enPhase, liveEvent: enLiveEvent } },
    });
    render(<LiveActivityStream events={FULL_FEED()} isStreaming />);
    const text = getList().textContent;
    expect(text).toContain('Fathoming your request');
    expect(text).toContain('Querying data completed · 847 rows · 1.2s');
    expect(text).toContain('Verification passed');
    expect(text).toContain('Finalizing your answer');
  });

  it('merges tool_call_started/finished into a single row', () => {
    const { container } = render(<LiveActivityStream events={TOOL_ROW()} isStreaming />);
    // The finished event absorbs the started row → exactly one row with a
    // checkmark label (no leftover "正在执行" spinner row).
    const text = getList().textContent;
    expect(text).toContain('Querying data完成 · 847行 · 1.2秒');
    expect(text).not.toContain('正在执行');
    // Count rendered event rows via the status icon span count.
    const rows = container.querySelectorAll('[class*="flex items-center gap-2 rounded-lg"]');
    expect(rows.length).toBe(1);
  });

  it('renders the phase headline with localized verb + title', () => {
    render(<LiveActivityStream events={FULL_FEED()} phase={{ state: 'goal', verb: 'goal', title: '' }} isStreaming />);
    const headline = getHeadline();
    expect(headline).toBeInTheDocument();
    expect(headline.textContent).toContain('正在理解');
    expect(headline.textContent).toContain('正在弄清你的需求');
  });

  it('renders artifact progress bar', () => {
    const events = [ev('artifact_progress', 'artifact_progress', { artifact_type: 'slide', current: 3, total: 12 })];
    const { container } = render(<LiveActivityStream events={events} isStreaming />);
    // 2026-08-31: scope to the PER-ROW bar inside the event list. The
    // stream-level ProgressBar (added 2026-08-25) renders first in DOM
    // order and enforces a 2% minimum width while running, so an unscoped
    // selector hit the wrong bar and saw '2%' instead of '25%'.
    const list = container.querySelector('[data-testid="live-activity-list"]');
    const bar = list.querySelector('[class*="overflow-hidden rounded-full bg-secondary"]');
    expect(bar).toBeInTheDocument();
    const fill = bar.querySelector('[class*="bg-blue-500"]');
    expect(fill).toBeInTheDocument();
    expect(fill.style.width).toBe('25%');
  });
});

// ---------------------------------------------------------------------------
// Streaming / collapse behavior
// ---------------------------------------------------------------------------

describe('LiveActivityStream — collapse behavior', () => {
  it('starts expanded while the stream is live', () => {
    render(<LiveActivityStream events={FULL_FEED()} isStreaming />);
    expect(getList()).toBeInTheDocument();
    expect(getSummary()).toBeNull();
  });

  it('starts collapsed for a finalized message (isStreaming false)', () => {
    render(<LiveActivityStream events={FULL_FEED()} />);
    expect(getSummary()).toBeInTheDocument();
    expect(getList()).toBeNull();
  });

  it('auto-collapses to a summary ~1s after the stream finishes', () => {
    vi.useFakeTimers();
    const { rerender } = render(<LiveActivityStream events={FULL_FEED()} isStreaming />);
    expect(getList()).toBeInTheDocument();

    rerender(<LiveActivityStream events={FULL_FEED()} isStreaming={false} />);
    // Still expanded immediately after the stream ends (grace period).
    expect(getList()).toBeInTheDocument();
    expect(getSummary()).toBeNull();

    act(() => { vi.advanceTimersByTime(1100); });
    expect(getSummary()).toBeInTheDocument();
    expect(getList()).toBeNull();
  });

  it('summary shows the per-turn audit trail: N events · M completed', () => {
    // FULL_FEED merges the tool pair → 4 rows, all terminal after the turn ends.
    render(<LiveActivityStream events={FULL_FEED()} />);
    const summary = getSummary();
    expect(summary).toBeInTheDocument();
    expect(summary.textContent).toContain('4 个事件 · 4 已完成');
  });

  it('summary excludes orphan running rows from the completed count', () => {
    // An interrupted turn: the last tool_call_started never finished → its
    // merged row stays "running" and is excluded from the completed count.
    const events = [...FULL_FEED(), ev('tool_call_started', 'tool_call_started', { tool_label: 'X' })];
    render(<LiveActivityStream events={events} />);
    expect(getSummary().textContent).toContain('5 个事件 · 4 已完成');
  });

  it('manual expand from the summary re-shows the list', () => {
    render(<LiveActivityStream events={FULL_FEED()} />);
    fireEvent.click(getSummary());
    expect(getList()).toBeInTheDocument();
    expect(getSummary()).toBeNull();
  });

  it('manual expand choice survives re-render without re-collapsing', () => {
    vi.useFakeTimers();
    const { rerender } = render(<LiveActivityStream events={FULL_FEED()} />);
    fireEvent.click(getSummary()); // user expands
    rerender(<LiveActivityStream events={FULL_FEED()} />);
    expect(getList()).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(1100); });
    expect(getList()).toBeInTheDocument(); // no auto-collapse after manual toggle
  });
});

// ---------------------------------------------------------------------------
// Thinking sub-section
// ---------------------------------------------------------------------------
// 2026-08-31: the collapsible Thinking block was REMOVED 2026-08-25 at the
// user's request (see the comment in LiveActivityStream.jsx near the footer;
// git commit 4a86311 has the removed implementation). Reasoning now streams
// in the one-line LiveStatusLine while the turn is live and is rendered in
// the bubble via ChatThinkingIndicator. These tests pin the current contract.

describe('LiveActivityStream — thinking sub-section', () => {
  it('renders nothing when no reasoning is provided', () => {
    render(<LiveActivityStream events={FULL_FEED()} isStreaming />);
    expect(screen.queryByTestId('live-activity-thinking-toggle')).toBeNull();
  });

  it('streams raw reasoning in the live status line instead of a thinking block', () => {
    render(
      <LiveActivityStream
        events={FULL_FEED()}
        streamingReasoning="step 1: parse request"
        isStreaming
      />,
    );
    // No collapsible thinking block in the feed anymore.
    expect(screen.queryByTestId('live-activity-thinking-toggle')).toBeNull();
    // While live, the one-line status bar shows the raw reasoning text.
    expect(screen.getByTestId('live-status-line').textContent).toContain('step 1: parse request');
  });

  it('hides the reasoning status line once the stream ends', () => {
    render(
      <LiveActivityStream
        events={FULL_FEED()}
        streamingReasoning="step 1: parse request"
      />,
    );
    expect(screen.queryByTestId('live-status-line')).toBeNull();
    expect(screen.queryByTestId('live-activity-thinking-toggle')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// SSE reconnect / live resume — appends while remaining expanded
// ---------------------------------------------------------------------------

describe('LiveActivityStream — live resume', () => {
  it('stays expanded as new events append during the live run', () => {
    const { rerender } = render(<LiveActivityStream events={FULL_FEED()} isStreaming />);
    const events = [...FULL_FEED(), ev('tool_call_started', 'tool_call_started', { tool_label: 'Build widget' })];
    rerender(<LiveActivityStream events={events} isStreaming />);
    expect(getList()).toBeInTheDocument();
    expect(getList().textContent).toContain('正在执行Build widget');
  });

  it('past-turn events inside the list have timestamps available as tooltips', () => {
    const { container } = render(<LiveActivityStream events={FULL_FEED()} isStreaming />);
    const rows = container.querySelectorAll('[title]');
    expect(rows.length).toBeGreaterThan(0);
    const row = within(getList());
    expect(row).toBeTruthy();
  });
});
// ---------------------------------------------------------------------------
// Legacy adapter (bug fix: unified rendering for all messages)
// ---------------------------------------------------------------------------

import { synthesizeLegacySteps, pickEvents } from './LiveActivityStream.legacyAdapter';

describe('LiveActivityStream — legacy adapter (bug fix)', () => {
  it('returns [] for empty / undefined input', () => {
    expect(synthesizeLegacySteps(null)).toEqual([]);
    expect(synthesizeLegacySteps(undefined)).toEqual([]);
    expect(synthesizeLegacySteps([])).toEqual([]);
  });

  it('maps status=done → tool_call_finished with row_count + duration passthrough', () => {
    const out = synthesizeLegacySteps([
      { number: 1, description: 'Querying data', status: 'done', row_count: 847, duration: 1.2 },
    ]);
    expect(out).toHaveLength(1);
    expect(out[0].type).toBe('tool_call_finished');
    expect(out[0].label_key).toBe('tool_call_finished');
    expect(out[0].params.tool_label).toBe('Querying data');
    expect(out[0].params.row_count).toBe(847);
    expect(out[0].params.duration).toBe(1.2);
    expect(out[0]._legacy).toBe(true);
  });

  it('maps status=failed → tool_call_failed', () => {
    const out = synthesizeLegacySteps([{ description: 'X', status: 'failed' }]);
    expect(out[0].type).toBe('tool_call_failed');
  });

  it('maps status=running → tool_call_started (orphan)', () => {
    const out = synthesizeLegacySteps([{ description: 'X', status: 'running' }]);
    expect(out[0].type).toBe('tool_call_started');
  });

  it('falls back to tool_name then numbered step label when description absent', () => {
    expect(synthesizeLegacySteps([{ tool_name: 'ask_data_agent' }])[0].params.tool_label)
      .toBe('ask_data_agent');
    expect(synthesizeLegacySteps([{ number: 7 }])[0].params.tool_label).toBe('step 7');
  });

  it('omits row_count / duration when not numeric', () => {
    const out = synthesizeLegacySteps([{ description: 'X', status: 'done', row_count: 'n/a' }]);
    expect(out[0].params).not.toHaveProperty('row_count');
  });

  it('pickEvents prefers live_events over legacySteps', () => {
    const live = [ev('phase_enter', 'phase_enter.goal')];
    const legacy = [{ description: 'X', status: 'done' }];
    expect(pickEvents(live, legacy)).toBe(live);
    expect(pickEvents([], legacy)).toEqual(synthesizeLegacySteps(legacy));
    expect(pickEvents(undefined, undefined)).toEqual([]);
  });

  it('renders legacySteps-only messages through the same LiveActivityStream shape', () => {
    const { container } = render(
      <LiveActivityStream
        legacySteps={[{ description: 'Querying data', status: 'done', row_count: 847 }]}
      />,
    );
    expect(container.querySelector('[data-testid="live-activity-summary"]')).toBeInTheDocument();
    fireEvent.click(container.querySelector('[data-testid="live-activity-summary"]'));
    expect(container.querySelector('[data-testid="live-activity-list"]')).toBeInTheDocument();
  });
});


// ---------------------------------------------------------------------------
// Headline unification — one source of truth
// ---------------------------------------------------------------------------

describe('LiveActivityStream — headline unification', () => {
  it('uses the latest phase_enter.* live event when present', () => {
    render(
      <LiveActivityStream
        events={[
          ev('phase_enter', 'phase_enter.goal'),
          ev('phase_enter', 'phase_enter.act'),
        ]}
        phase={{ state: 'goal', verb: 'Legacy', title: 'should not show' }}
        isStreaming
      />,
    );
    const text = screen.getByTestId('live-activity-headline').textContent;
    expect(text).toContain('正在构建');
    expect(text).not.toContain('Legacy');
    expect(text).not.toContain('should not show');
  });

  it('falls back to legacy phase when no phase_enter events exist', () => {
    // When the localized template map has a verb/title for the state, it wins
    // over the raw `phase.verb`/`phase.title` props — that's the whole point
    // of the unified source: the template map is the source of truth.
    render(
      <LiveActivityStream
        events={[ev('tool_call_started', 'tool_call_started', { tool_label: 'X' })]}
        phase={{ state: 'act', verb: 'Legacy Act', title: 'Fallback title' }}
        isStreaming
      />,
    );
    const text = screen.getByTestId('live-activity-headline').textContent;
    expect(text).toContain('正在构建');
    expect(text).toContain('正在产出交付物');
  });

  it('uses raw phase.verb when the state has no localized template', () => {
    // For a custom state name that's not in t.chat.phase, the legacy prop
    // surfaces directly — that's the genuine "fallback" path.
    render(
      <LiveActivityStream
        events={[ev('tool_call_started', 'tool_call_started', { tool_label: 'X' })]}
        phase={{ state: 'custom_state', verb: 'Doing something custom', title: '' }}
        isStreaming
      />,
    );
    const text = screen.getByTestId('live-activity-headline').textContent;
    expect(text).toContain('Doing something custom');
  });

  it('hides the headline bar when no phase signal exists at all', () => {
    render(
      <LiveActivityStream
        events={[ev('tool_call_started', 'tool_call_started', { tool_label: 'X' })]}
        isStreaming
      />,
    );
    expect(screen.queryByTestId('live-activity-headline')).toBeNull();
  });
});// ---------------------------------------------------------------------------
// Sub-agent delegation badge
// ---------------------------------------------------------------------------

describe('LiveActivityStream — sub-agent badge', () => {
  it('merges invoked + returned into a single row that flips spinner → checkmark', () => {
    const events = [
      ev('subagent_invoked', 'subagent_invoked',
        { agent_label: 'Querying data', target: 'ask_data_agent' }),
      ev('subagent_returned', 'subagent_returned',
        { agent_label: 'Querying data', duration: 1.2, row_count: 847 }),
    ];
    render(<LiveActivityStream events={events} isStreaming />);
    const rows = screen.getAllByTestId('subagent-row');
    expect(rows.length).toBe(1);
    expect(rows[0].textContent).toContain('Querying data已返回');
    expect(rows[0].textContent).toContain('1.2');
    // The target badge (tool name) is preserved on the merged row.
    expect(rows[0].textContent).toContain('ask_data_agent');
  });

  it('keeps an invoked row in "running" state when the return event never arrives', () => {
    const events = [
      ev('subagent_invoked', 'subagent_invoked',
        { agent_label: 'Querying data', target: 'ask_data_agent' }),
    ];
    render(<LiveActivityStream events={events} isStreaming />);
    const rows = screen.getAllByTestId('subagent-row');
    expect(rows.length).toBe(1);
    expect(rows[0].textContent).toContain('委托给Querying data');
  });

  it('renders in English', () => {
    useLanguageMock.mockReturnValue({
      lang: 'en', setLang: () => {},
      t: { chat: { phase: enPhase, liveEvent: enLiveEvent } },
    });
    render(
      <LiveActivityStream
        events={[
          ev('subagent_invoked', 'subagent_invoked',
            { agent_label: 'Querying data', target: 'ask_data_agent' }),
          ev('subagent_returned', 'subagent_returned',
            { agent_label: 'Querying data', duration: 1.2 }),
        ]}
        isStreaming
      />,
    );
    const text = screen.getAllByTestId('subagent-row')[0].textContent;
    expect(text).toContain('Querying data returned');
    // Once the return event arrived, the merged row shows the "returned" label
    // (not the "delegating" label) — that's the spinner→checkmark transition.
    expect(text).not.toContain('Delegating to');
    expect(text).toContain('ask_data_agent');
  });
});


// ---------------------------------------------------------------------------
// Plan summary card
// ---------------------------------------------------------------------------

describe('LiveActivityStream — plan summary card', () => {
  it('renders the plan_summary event as a card with step list', () => {
    const events = [
      ev('plan_summary', 'plan_summary', {
        n: 3,
        steps: ['Inspect data source', 'Build widgets', 'Verify outputs'],
      }),
    ];
    render(<LiveActivityStream events={events} isStreaming />);
    const row = screen.getByTestId('plan-summary-row');
    expect(row).toBeInTheDocument();
    expect(row.textContent).toContain('计划已就绪');
    expect(row.textContent).toContain('Inspect data source');
    expect(row.textContent).toContain('Build widgets');
    expect(row.textContent).toContain('Verify outputs');
  });

  it('omits step list when steps empty', () => {
    render(
      <LiveActivityStream
        events={[ev('plan_summary', 'plan_summary', { n: 1, steps: [] })]}
        isStreaming
      />,
    );
    const row = screen.getByTestId('plan-summary-row');
    expect(row).toBeInTheDocument();
    expect(row.querySelector('ul')).toBeNull();
  });
});


// ---------------------------------------------------------------------------
// Inline data preview pane (data_offer → attached to tool row)
// ---------------------------------------------------------------------------

describe('LiveActivityStream — data offer inline preview', () => {
  const TOOL_WITH_OFFER = () => [
      ev('tool_call_started', 'tool_call_started', { tool_label: 'Querying data' }),
      ev('tool_call_finished', 'tool_call_finished',
        { tool_label: 'Querying data', row_count: 847, duration: 1.2 }),
      ev('data_offer', 'data_offer', {
        tool_label: 'Querying data', row_count: 847,
        columns: ['product', 'qty'],
        sample_rows: [
          { product: 'ethylene', qty: 100 },
          { product: 'propylene', qty: 80 },
        ],
      }),
    ];

  it('renders a Show toggle for finished tools that came with a data_offer', () => {
    const { container } = render(<LiveActivityStream events={TOOL_WITH_OFFER()} isStreaming={false} />);
    fireEvent.click(container.querySelector('[data-testid="live-activity-summary"]'));
    const toggle = container.querySelector('[data-testid="data-preview-toggle"]');
    expect(toggle).toBeTruthy();
    expect(toggle.textContent).toContain('847');
  });

  it('clicking the toggle reveals the inline preview', () => {
    const { container } = render(<LiveActivityStream events={TOOL_WITH_OFFER()} isStreaming={false} />);
    fireEvent.click(container.querySelector('[data-testid="live-activity-summary"]'));
    const toggle = container.querySelector('[data-testid="data-preview-toggle"]');
    fireEvent.click(toggle);
    const preview = container.querySelector('[data-testid="data-preview"]');
    expect(preview).toBeTruthy();
    const text = preview.textContent;
    expect(text).toContain('ethylene');
    expect(text).toContain('propylene');
    expect(text).toContain('product');
    expect(text).toContain('qty');
  });

  it('clicking the toggle again hides the preview', () => {
    const { container } = render(<LiveActivityStream events={TOOL_WITH_OFFER()} isStreaming={false} />);
    fireEvent.click(container.querySelector('[data-testid="live-activity-summary"]'));
    const toggle = container.querySelector('[data-testid="data-preview-toggle"]');
    fireEvent.click(toggle);
    fireEvent.click(toggle);
    expect(container.querySelector('[data-testid="data-preview"]')?.querySelector('table')).toBeNull();
  });

  it('does NOT render a preview when no data_offer event arrived', () => {
    render(<LiveActivityStream events={FULL_FEED()} isStreaming />);
    expect(screen.queryByTestId('data-preview-toggle')).toBeNull();
  });
});


// ---------------------------------------------------------------------------
// Live duration ticker on running rows
// ---------------------------------------------------------------------------

describe('LiveActivityStream — live duration ticker', () => {
  it('renders a ticking timer on running rows', () => {
    vi.useFakeTimers();
    const past = new Date(Date.now() - 3000).toISOString();
    const events = [
      ev('tool_call_started', 'tool_call_started', { tool_label: 'X' }, { ts: past }),
    ];
    render(<LiveActivityStream events={events} isStreaming />);
    const list = getList();
    expect(list.textContent).toMatch(/3s/);
    act(() => { vi.advanceTimersByTime(2000); });
    expect(getList().textContent).toMatch(/5s/);
  });

  it('does NOT mount the ticker on a finished row', () => {
    const { container } = render(<LiveActivityStream
      events={[
        ev('tool_call_started', 'tool_call_started', { tool_label: 'X' }),
        ev('tool_call_finished', 'tool_call_finished',
          { tool_label: 'X', row_count: 5, duration: 1.2 }),
      ]}
    />);
    fireEvent.click(container.querySelector('[data-testid="live-activity-summary"]'));
    const text = container.querySelector('[data-testid="live-activity-list"]').textContent;
    expect(text).toContain('1.2s');
  });
});


// ---------------------------------------------------------------------------
// finalize_done — "Finalizing your answer" must not spin/tick forever
// ---------------------------------------------------------------------------

describe('LiveActivityStream — finalize row terminality (bug fix)', () => {
  const FINALIZE_PAIR = () => [
    ev('finalize_started', 'finalize_started'),
    ev('finalize_done', 'finalize_done'),
  ];

  it('finalize_done flips the finalize_started row to done (no spinner, no ticker)', () => {
    const { container } = render(<LiveActivityStream events={FINALIZE_PAIR()} isStreaming />);
    const list = getList();
    expect(list.textContent).toContain('正在生成最终答复');
    // Row is done → no spinning loader, no live second counter.
    expect(container.querySelector('.animate-spin')).toBeNull();
    expect(container.querySelector('.tabular-nums')).toBeNull();
  });

  it('finalize_started alone after the stream ends renders done (legacy turns)', () => {
    const { container } = render(<LiveActivityStream events={FULL_FEED()} />);
    // Stream is over → auto-collapsed; expand to inspect the rows.
    fireEvent.click(getSummary());
    const list = getList();
    expect(list.textContent).toContain('正在生成最终答复');
    // A stale 'running' finalize row must not keep spinning / ticking forever.
    expect(container.querySelector('.animate-spin')).toBeNull();
    expect(container.querySelector('.tabular-nums')).toBeNull();
  });

  it('finalize_done is pure metadata — it does not add a row', () => {
    const { container } = render(<LiveActivityStream events={FINALIZE_PAIR()} isStreaming />);
    const rows = container.querySelectorAll('[class*="flex items-center gap-2 rounded-lg"]');
    expect(rows.length).toBe(1);
  });

  it('finalize_done also closes a trailing artifact_progress row', () => {
    const { container } = render(
      <LiveActivityStream
        events={[
          ev('finalize_started', 'finalize_started'),
          ev('artifact_progress', 'artifact_progress'),
          ev('finalize_done', 'finalize_done'),
        ]}
        isStreaming
      />
    );
    // Both finalize-phase rows are closed → no spinner, no ticking timer.
    expect(container.querySelector('.animate-spin')).toBeNull();
    expect(container.querySelector('.tabular-nums')).toBeNull();
  });

  it('artifact_progress left running after stream end renders done (legacy turns)', () => {
    const { container } = render(
      <LiveActivityStream
        events={[ev('finalize_started', 'finalize_started'), ev('artifact_progress', 'artifact_progress')]}
      />
    );
    fireEvent.click(getSummary());
    expect(container.querySelector('.animate-spin')).toBeNull();
    expect(container.querySelector('.tabular-nums')).toBeNull();
  });
});


// ---------------------------------------------------------------------------
// Retry lineage indent
// ---------------------------------------------------------------------------

describe('LiveActivityStream — retry lineage indent', () => {
  it('indents rows that come after a retry event', () => {
    const events = [
      ev('tool_call_started', 'tool_call_started', { tool_label: 'first' }),
      ev('tool_call_failed', 'tool_call_failed', { tool_label: 'first' }),
      ev('retry', 'retry', { target: 'first' }),
      ev('tool_call_started', 'tool_call_started', { tool_label: 'retry-1' }),
      ev('tool_call_finished', 'tool_call_finished',
        { tool_label: 'retry-1', row_count: 3, duration: 0.5 }),
    ];
    render(<LiveActivityStream events={events} />);
    fireEvent.click(screen.getByTestId('live-activity-summary'));
    const list = getList();
    expect(list.textContent).toContain('retry-1');
    const indentDivs = list.querySelectorAll('.ml-4');
    expect(indentDivs.length).toBeGreaterThanOrEqual(1);
  });

  it('caps indent depth at 2', () => {
    // Two consecutive retries (depth opens 1, then 2), followed by a
    // successful retry attempt — produces indent=2 rows below the failure.
    const events = [
      ev('tool_call_started', 'tool_call_started', { tool_label: 'first' }),
      ev('tool_call_failed', 'tool_call_failed', { tool_label: 'first' }),
      ev('retry', 'retry', { target: 'first' }),
      ev('retry', 'retry', { target: 'first-again' }),
      ev('tool_call_started', 'tool_call_started', { tool_label: 'retry-final' }),
      ev('tool_call_finished', 'tool_call_finished',
        { tool_label: 'retry-final', row_count: 3, duration: 0.5 }),
    ];
    const { container } = render(<LiveActivityStream events={events} />);
    fireEvent.click(container.querySelector('[data-testid="live-activity-summary"]'));
    const list = container.querySelector('[data-testid="live-activity-list"]');
    expect(list.querySelectorAll('.ml-8').length).toBeGreaterThanOrEqual(1);
    expect(list.querySelectorAll('.ml-12, .ml-16').length).toBe(0);
  });
});


// ---------------------------------------------------------------------------
// LIVE badge lifecycle
// ---------------------------------------------------------------------------

describe('LiveActivityStream — LIVE badge lifecycle', () => {
  it('shows the LIVE badge while streaming', () => {
    const { container } = render(<LiveActivityStream events={FULL_FEED()} isStreaming />);
    // The LIVE badge is in the footer (below the event list), so query the
    // stream wrapper rather than the list.
    expect(container.querySelector('[data-testid="live-activity-stream"]').textContent).toContain('实时');
  });

  it('hides the LIVE badge after streaming ends (footer shows summary instead)', () => {
    vi.useFakeTimers();
    const { rerender } = render(<LiveActivityStream events={FULL_FEED()} isStreaming />);
    rerender(<LiveActivityStream events={FULL_FEED()} isStreaming={false} />);
    act(() => { vi.advanceTimersByTime(1100); });
    expect(screen.getByTestId('live-activity-summary')).toBeInTheDocument();
  });
});