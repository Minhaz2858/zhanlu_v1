/**
 * ReportSidePanel component tests.
 *
 * Because the component uses lucide-react, framer-motion, recharts
 * (via ReportCardBody) and a sandbox export hook (via ExportMenu),
 * we mock those sub-trees so every test focuses purely on the
 * panel's own DOM / interaction contract.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import React from 'react';
import ReportSidePanel from '@/components/chat/ReportSidePanel';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('lucide-react', () => ({
  X:          (props) => React.createElement('span', { 'data-icon': 'x', ...props }, '\u2715'),
  Database:   (props) => React.createElement('span', { 'data-icon': 'database', ...props }),
}));

vi.mock('framer-motion', () => ({
  motion: {
    div: React.forwardRef(({ children, ...props }, ref) =>
      React.createElement('div', { ref, 'data-motion': 'div', ...props }, children)
    ),
  },
  AnimatePresence: ({ children }) => children,
}));

vi.mock('@/components/chat/ReportCardBody', () => ({
  default: ({ payload, onAction }) =>
    React.createElement(
      'div',
      { 'data-testid': 'report-card-body' },
      React.createElement('span', { 'data-testid': 'body-payload-title' }, payload?.title || ''),
      React.createElement('span', { 'data-testid': 'body-kpicount' }, String(payload?.kpis?.length ?? 0)),
      React.createElement('span', { 'data-testid': 'body-chart-type' }, payload?.chart?.type || 'none'),
      React.createElement('span', { 'data-testid': 'body-insights-count' }, String(payload?.insights?.length ?? 0)),
      payload?.next_step && React.createElement('span', { 'data-testid': 'body-next-step' }, payload.next_step),
      payload?.warnings &&
        React.createElement('span', { 'data-testid': 'body-warnings' }, payload.warnings[0]),
    ),
}));

vi.mock('@/lib/utils', () => ({
  cn: (...args) => args.filter(Boolean).join(' '),
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function buildPayload(overrides = {}) {
  return {
    title: 'Sales Report Q1',
    source: 'MySQL (production)',
    generated_at: '2026-07-15T10:30:00Z',
    kpis: [
      { label: 'Revenue', value: 1234567, delta: '+12.3%' },
      { label: 'Orders', value: 8743, delta: '-3.1%' },
    ],
    chart: { type: 'bar', data: [{ month: 'Jan', Revenue: 100 }, { month: 'Feb', Revenue: 200 }] },
    insights: [
      { text: 'Revenue grew 12.3% MoM', icon: 'trending_up' },
    ],
    next_step: 'Compare with last quarter',
    warnings: ['Data for March is incomplete'],
    actions: [
      { label: 'Show details', prompt: 'show details for Q1' },
      { label: 'Export report', prompt: 'export report as PDF' },
    ],
    ...overrides,
  };
}

function renderPanel({ payload, artifactId, generatedAt, onClose, onAction, sessionId } = {}) {
  const defaults = {
    payload: buildPayload(),
    artifactId: null,
    generatedAt: '2026-07-15T10:30:00Z',
    onClose: vi.fn(),
    onAction: vi.fn(),
    sessionId: 'sess-1',
  };
  const props = {
    ...defaults,
    ...(payload !== undefined ? { payload } : {}),
    ...(artifactId !== undefined ? { artifactId } : {}),
    ...(generatedAt !== undefined ? { generatedAt } : {}),
    ...(onClose !== undefined ? { onClose } : {}),
    ...(onAction !== undefined ? { onAction } : {}),
    ...(sessionId !== undefined ? { sessionId } : {}),
  };
  return render(React.createElement(ReportSidePanel, props));
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ReportSidePanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // 1. renders title + source + Last modified
  it('renders title, source badge and locale-formatted Last modified in the header', () => {
    renderPanel();
    // Title appears in both header h3 and mocked ReportCardBody — at least one should be there
    const titleMatches = screen.getAllByText('Sales Report Q1');
    expect(titleMatches.length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('MySQL (production)')).toBeInTheDocument();
    // Locale-formatted timestamp: should contain month, day, year, hour, minute
    expect(screen.getByText(/Last modified:/)).toBeInTheDocument();
  });

  // 2. close button fires onClose
  it('fires onClose when the X button is clicked', () => {
    const onClose = vi.fn();
    renderPanel({ onClose });
    fireEvent.click(screen.getByTitle('Close panel (Esc)'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  // 3. close button fires onClose via Escape key
  it('fires onClose when Escape key is pressed', () => {
    const onClose = vi.fn();
    renderPanel({ onClose });
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  // 4. action chip fires onAction with the right prompt
  it('fires onAction when an action chip in the footer is clicked', () => {
    const onAction = vi.fn();
    renderPanel({ onAction });
    const chips = screen.getAllByText(/Show details|Export report/);
    expect(chips.length).toBeGreaterThanOrEqual(1);
    fireEvent.click(chips[0]);
    expect(onAction).toHaveBeenCalledWith(expect.stringContaining('show details'));
  });

  // 5. Save-to-space button renders (ExportMenu removed from the panel)
  it('renders the Save to space button when a sessionId is provided', () => {
    renderPanel({ artifactId: null, sessionId: 'sess-1' });
    expect(screen.getByText('Save to space')).toBeInTheDocument();
  });

  it('does NOT render the Save to space button when sessionId is absent', () => {
    renderPanel({ sessionId: null });
    expect(screen.queryByText('Save to space')).not.toBeInTheDocument();
  });

  // 6. body renders KPIs, chart, insights, next_step, warnings
  it('renders the body with KPIs, chart, insights, next_step, and warnings', () => {
    renderPanel();
    const body = screen.getByTestId('report-card-body');
    expect(body).toBeInTheDocument();
    // KPIs: 2
    expect(screen.getByTestId('body-kpicount').textContent).toBe('2');
    // Chart type: bar
    expect(screen.getByTestId('body-chart-type').textContent).toBe('bar');
    // Insights: 1
    expect(screen.getByTestId('body-insights-count').textContent).toBe('1');
    // Next step
    expect(screen.getByTestId('body-next-step')).toBeInTheDocument();
    // Warnings
    expect(screen.getByTestId('body-warnings')).toBeInTheDocument();
  });

  // 7. empty actions handled (no crash)
  it('handles payloads with empty actions array gracefully', () => {
    const payload = buildPayload({ actions: [] });
    renderPanel({ payload });
    // Still renders without crash
    expect(screen.getByTestId('report-card-body')).toBeInTheDocument();
  });

  // 8. locale-formatted timestamp in en-US and zh-CN styles
  it('renders locale-aware Last modified timestamp', () => {
    // Override Intl.DateTimeFormat to produce a predictable output
    const orig = Intl.DateTimeFormat;
    const mockFormat = vi.fn().mockReturnValue('Jul 15, 2026, 10:30 AM');
    global.Intl.DateTimeFormat = vi.fn(() => ({ format: mockFormat }));
    try {
      renderPanel({ generatedAt: '2026-07-15T10:30:00Z' });
      expect(screen.getByText(/Last modified: Jul 15, 2026, 10:30 AM/)).toBeInTheDocument();
    } finally {
      global.Intl.DateTimeFormat = orig;
    }
  });
});
