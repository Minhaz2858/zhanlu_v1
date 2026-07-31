import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act, cleanup } from '@testing-library/react';
import DashboardViewer from './DashboardViewer';
import * as dashApi from '@/api/dashboards';

// recharts ResponsiveContainer needs ResizeObserver (absent in jsdom); stub the
// chart primitives so tests focus on data + poll lifecycle, not chart rendering.
vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }) => <div data-testid="rc">{children}</div>,
  BarChart: ({ children }) => <div>{children}</div>,
  LineChart: ({ children }) => <div>{children}</div>,
  PieChart: ({ children }) => <div>{children}</div>,
  Bar: () => <div />,
  Line: () => <div />,
  Pie: () => <div />,
  Cell: () => <div />,
  XAxis: () => <div />,
  YAxis: () => <div />,
  CartesianGrid: () => <div />,
  Tooltip: () => <div />,
  Legend: () => <div />,
}));

vi.mock('@/api/dashboards', () => ({ queryDashboard: vi.fn() }));

// NOTE: we use fake timers to control the poll interval, so we must NOT use
// waitFor/findByText (their internal polling timer is faked and hangs). Instead
// flush microtasks with vi.advanceTimersByTimeAsync, then assert synchronously.
beforeEach(() => {
  vi.useFakeTimers();
  dashApi.queryDashboard.mockClear();
});
afterEach(() => { vi.useRealTimers(); cleanup(); });

const dash = {
  id: 'd1',
  name: 'Sales',
  refresh_interval_seconds: 30,
  definition: { widgets: [
    { id: 'w1', type: 'kpi', title: 'Revenue', sql: 'SELECT 1', options: { value_column: 'n' } },
    { id: 'w2', type: 'line', title: 'Trend', sql: 'SELECT 1', options: { x_column: 'd', y_column: 'v' } },
  ] },
};

function okRes(n) {
  return {
    results: {
      w1: { columns: ['n'], rows: [{ n }], error: null, truncated: false },
      w2: { columns: [], rows: [], error: null, truncated: false },
    },
    refreshed_at: '2026-07-29T00:00:00Z',
  };
}

it('renders widgets and fetches on mount', async () => {
  dashApi.queryDashboard.mockResolvedValue(okRes(42));
  render(<DashboardViewer dashboard={dash} polling />);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getByText('Revenue')).toBeInTheDocument();
  expect(screen.getByText('42')).toBeInTheDocument();
});

it('isolates per-widget errors', async () => {
  dashApi.queryDashboard.mockResolvedValue({ results: {
    w1: { columns: ['n'], rows: [{ n: 7 }], error: null, truncated: false },
    w2: { columns: [], rows: [], error: 'bad sql', truncated: false },
  }, refreshed_at: '2026-07-29T00:00:00Z' });
  render(<DashboardViewer dashboard={dash} polling />);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getByText('7')).toBeInTheDocument();
  expect(screen.getByText(/bad sql/i)).toBeInTheDocument();
});

it('polls on interval', async () => {
  dashApi.queryDashboard.mockResolvedValue(okRes(1));
  render(<DashboardViewer dashboard={dash} polling />);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(dashApi.queryDashboard).toHaveBeenCalledTimes(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
  expect(dashApi.queryDashboard).toHaveBeenCalledTimes(2);
});

it('stops polling on unmount', async () => {
  dashApi.queryDashboard.mockResolvedValue(okRes(1));
  const { unmount } = render(<DashboardViewer dashboard={dash} polling />);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  unmount();
  const calls = dashApi.queryDashboard.mock.calls.length;
  await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
  expect(dashApi.queryDashboard.mock.calls.length).toBe(calls);
});

it('shows skeletons on first load', async () => {
  dashApi.queryDashboard.mockReturnValue(new Promise(() => {}));
  render(<DashboardViewer dashboard={dash} polling={false} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getAllByTestId('widget-skeleton')).toHaveLength(2);
});

it('shows empty state when widgets return no rows', async () => {
  dashApi.queryDashboard.mockResolvedValue({ results: {
    w1: { columns: ['n'], rows: [], error: null, truncated: false },
    w2: { columns: [], rows: [], error: null, truncated: false },
  }, refreshed_at: '2026-07-29T00:00:00Z' });
  render(<DashboardViewer dashboard={dash} polling={false} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getAllByText(/no data yet/i)).toHaveLength(2);
});

it('formats KPI values with compact notation and unit', async () => {
  const kpiDash = { ...dash, definition: { widgets: [
    { id: 'w1', type: 'kpi', title: 'Revenue', sql: 'SELECT 1', options: { value_column: 'n', unit: 'USD' } },
  ] } };
  dashApi.queryDashboard.mockResolvedValue({ results: {
    w1: { columns: ['n'], rows: [{ n: 12400 }], error: null, truncated: false },
  }, refreshed_at: '2026-07-29T00:00:00Z' });
  render(<DashboardViewer dashboard={kpiDash} polling={false} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getByText('12.4K USD')).toBeInTheDocument();
});

it('renders a delta chip when compare_column is set', async () => {
  const kpiDash = { ...dash, definition: { widgets: [
    { id: 'w1', type: 'kpi', title: 'Revenue', sql: 'SELECT 1', options: { value_column: 'n', compare_column: 'prev' } },
  ] } };
  dashApi.queryDashboard.mockResolvedValue({ results: {
    w1: { columns: ['n', 'prev'], rows: [{ n: 120, prev: 100 }], error: null, truncated: false },
  }, refreshed_at: '2026-07-29T00:00:00Z' });
  render(<DashboardViewer dashboard={kpiDash} polling={false} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getByText(/20%/)).toBeInTheDocument();
});
