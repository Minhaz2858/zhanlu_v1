import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { LanguageProvider } from '@/lib/LanguageProvider';
import ScheduledPanel from './ScheduledPanel';

beforeEach(() => {
  localStorage.setItem('zhanlu_lang', 'en');
  globalThis.fetch = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function mockBySession({ task, executions = [] } = {}) {
  globalThis.fetch.mockResolvedValueOnce({
    ok: true,
    json: async () => ({ task, executions }),
  });
}

function renderPanel(props) {
  return render(
    <MemoryRouter>
      <LanguageProvider>
        <ScheduledPanel
          sessionId="session-1"
          open
          onOpenChange={() => {}}
          focusExecutionId={null}
          {...props}
        />
      </LanguageProvider>
    </MemoryRouter>,
  );
}

describe('ScheduledPanel', () => {
  it('shows the empty state when the session has no task', async () => {
    mockBySession({ task: null, executions: [] });
    renderPanel();
    expect(
      await screen.findByText(/isn't tied to a scheduled task/i),
    ).toBeTruthy();
  });

  it('renders the task name and next run time', async () => {
    mockBySession({
      task: {
        id: 'task-1',
        name: 'mikae weekly report',
        schedule: 'FREQ=DAILY;BYHOUR=8;BYMINUTE=0',
        next_run_at: '2026-07-28T08:00:00Z',
        skip_confirmation: true,
        status: 'active',
      },
      executions: [],
    });
    renderPanel();
    expect(await screen.findByText(/mikae weekly report/)).toBeTruthy();
    expect(await screen.findByText(/Daily at 08:00/i)).toBeTruthy();
  });

  it('renders each past run with date header, status, and excerpt', async () => {
    mockBySession({
      task: {
        id: 'task-1',
        name: 'mikae weekly report',
        schedule: 'FREQ=DAILY;BYHOUR=8;BYMINUTE=0',
        next_run_at: '2026-07-28T08:00:00Z',
        skip_confirmation: true,
        status: 'active',
      },
      executions: [
        {
          id: 'exec-1',
          status: 'completed',
          output_text: 'I have prepared your weekly C5 product price report for the week ending June 27, 2026.',
          started_at: '2026-07-27T08:00:00Z',
          completed_at: '2026-07-27T08:02:13Z',
          files: [],
        },
        {
          id: 'exec-2',
          status: 'completed',
          output_text: 'I have prepared your weekly C5 product price report for the week ending June 26, 2026.',
          started_at: '2026-07-26T08:00:00Z',
          completed_at: '2026-07-26T08:01:55Z',
          files: [],
        },
      ],
    });
    renderPanel();
    expect(await screen.findByText(/Jul 27/)).toBeTruthy();
    expect(await screen.findByText(/Jul 26/)).toBeTruthy();
    expect(await screen.findByText(/week ending June 27/)).toBeTruthy();
  });

  it('expands a run when clicked, showing its files', async () => {
    mockBySession({
      task: {
        id: 'task-1',
        name: 't',
        schedule: 'FREQ=DAILY;BYHOUR=8;BYMINUTE=0',
        next_run_at: '2026-07-28T08:00:00Z',
        skip_confirmation: true,
        status: 'active',
      },
      executions: [
        {
          id: 'exec-1',
          status: 'completed',
          output_text: 'Report ready.',
          started_at: '2026-07-27T08:00:00Z',
          completed_at: '2026-07-27T08:02:13Z',
          files: [
            { id: 'f-1', name: 'report.pdf', file_type: 'pdf', file_url: '/files/report.pdf' },
          ],
        },
      ],
    });
    renderPanel();
    const runButton = await screen.findByText(/Jul 27/);
    fireEvent.click(runButton);
    expect(await screen.findByText(/report\.pdf/)).toBeTruthy();
  });

  it('renders the recovery card on failed runs', async () => {
    mockBySession({
      task: {
        id: 'task-1',
        name: 't',
        schedule: 'FREQ=DAILY;BYHOUR=8;BYMINUTE=0',
        next_run_at: '2026-07-28T08:00:00Z',
        skip_confirmation: false,
        status: 'active',
      },
      executions: [
        {
          id: 'exec-1',
          status: 'failed',
          error: 'Provider 429: quota exceeded',
          error_code: 'quota',
          started_at: '2026-07-27T08:00:00Z',
          completed_at: '2026-07-27T08:01:00Z',
          files: [],
        },
      ],
    });
    renderPanel();
    const runButton = await screen.findByText(/Jul 27/);
    fireEvent.click(runButton);
    expect(await screen.findByText(/Open cost settings/i)).toBeTruthy();
  });

  it('auto-expands the focused execution passed via focusExecutionId', async () => {
    mockBySession({
      task: {
        id: 'task-1',
        name: 't',
        schedule: 'FREQ=DAILY;BYHOUR=8;BYMINUTE=0',
        next_run_at: '2026-07-28T08:00:00Z',
        skip_confirmation: false,
        status: 'active',
      },
      executions: [
        {
          id: 'exec-1',
          status: 'completed',
          output_text: 'Focused run content.',
          started_at: '2026-07-27T08:00:00Z',
          completed_at: '2026-07-27T08:02:13Z',
          files: [],
        },
        {
          id: 'exec-2',
          status: 'completed',
          output_text: 'Other run content.',
          started_at: '2026-07-26T08:00:00Z',
          completed_at: '2026-07-26T08:02:13Z',
          files: [],
        },
      ],
    });
    renderPanel({ focusExecutionId: 'exec-2' });
    // The focused run should be auto-expanded, showing its full output_text
    // in the detail panel. Use a getAllBy query because the same text also
    // appears as the list-item excerpt; pick the one with the expanded class.
    const matches = await screen.findAllByText(/Other run content/);
    const detail = matches.find((el) => el.className.includes('whitespace-pre-wrap'));
    expect(detail).toBeTruthy();
  });

  it('truncates long output text at a word boundary without crashing', async () => {
    const longText =
      'This is a very long run output that exceeds the one hundred and forty character excerpt limit so that the truncation code path is exercised. '.repeat(2)
        .trim();
    mockBySession({
      task: {
        id: 'task-1',
        name: 't',
        schedule: 'FREQ=DAILY;BYHOUR=8;BYMINUTE=0',
        next_run_at: '2026-07-28T08:00:00Z',
        skip_confirmation: true,
        status: 'active',
      },
      executions: [
        {
          id: 'exec-1',
          status: 'completed',
          output_text: longText,
          started_at: '2026-07-27T08:00:00Z',
          completed_at: '2026-07-27T08:02:13Z',
          files: [],
        },
      ],
    });
    renderPanel();
    // The panel must not crash on long output_text (regression: rsplit is not a
    // JS function). The excerpt should contain an ellipsis and be shorter than
    // the full text. "hundred" is a word safely inside the truncated region.
    const excerptEl = await screen.findByText(/hundred/);
    expect(excerptEl.textContent).toContain('…');
    expect(excerptEl.textContent.length).toBeLessThan(longText.length);
  });

  it('maps weekly BYDAY codes to day names in the schedule label', async () => {
    mockBySession({
      task: {
        id: 'task-1',
        name: 't',
        schedule: 'FREQ=WEEKLY;BYDAY=MO,WE,FR;BYHOUR=9;BYMINUTE=0',
        next_run_at: '2026-07-28T09:00:00Z',
        skip_confirmation: true,
        status: 'active',
      },
      executions: [],
    });
    renderPanel();
    expect(await screen.findByText(/Mon, Wed, Fri at 09:00/i)).toBeTruthy();
  });

  it('renders the outer aside as a docked flex container without overlay positioning classes', async () => {
    // TDD red: current aside carries the fixed-overlay classes
    // ('fixed right-0 top-0 z-40 h-screen w-[420px] ... shadow-2xl ...
    // translate-x-full'). After the wrapper refactor it becomes
    // 'flex h-full w-full flex-col border-l border-border bg-card' and
    // this test passes.
    mockBySession({
      task: { id: 'task-1', name: 't', status: 'active', schedule: 'FREQ=DAILY;BYHOUR=8;BYMINUTE=0', next_run_at: '2026-07-28T08:00:00Z' },
      executions: [],
    });
    const { container } = renderPanel();
    const aside = container.querySelector('aside');
    expect(aside).not.toBeNull();
    const cls = aside.className;
    // New docked contract: flex column that fills its <Panel> cell and
    // reads as a separate column via the left border + card surface.
    expect(cls).toMatch(/\bflex\b/);
    expect(cls).toMatch(/\bh-full\b/);
    expect(cls).toMatch(/\bw-full\b/);
    expect(cls).toMatch(/\bflex-col\b/);
    expect(cls).toMatch(/\bborder-l\b/);
    expect(cls).toMatch(/\bbg-card\b/);
    // Old overlay contract MUST NOT be present.
    expect(cls).not.toMatch(/\bfixed\b/);
    expect(cls).not.toMatch(/z-40/);
    expect(cls).not.toMatch(/shadow-2xl/);
    expect(cls).not.toMatch(/translate-x/);
    expect(cls).not.toMatch(/h-screen/);
    expect(cls).not.toMatch(/w-\[420px\]/);
  });

  it('calls onClose when the close button is pressed', async () => {
    // TDD red: current implementation calls onOpenChange?.(false).
    // After refactor it calls onClose?.() and this test passes.
    mockBySession({
      task: { id: 'task-1', name: 't', status: 'active', schedule: 'FREQ=DAILY;BYHOUR=8;BYMINUTE=0', next_run_at: '2026-07-28T08:00:00Z' },
      executions: [],
    });
    const onClose = vi.fn();
    renderPanel({ onClose });
    const closeBtn = await screen.findByLabelText(/Close scheduled panel/i);
    fireEvent.click(closeBtn);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
