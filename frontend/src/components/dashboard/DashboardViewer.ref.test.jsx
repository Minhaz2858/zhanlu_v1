import { render, act } from '@testing-library/react';
import { vi } from 'vitest';
import DashboardViewer from './DashboardViewer';

vi.mock('@/api/dashboards', () => ({
  queryDashboard: vi.fn().mockResolvedValue({ results: {}, refreshed_at: '2026-07-30T00:00:00Z' }),
  updateDashboard: vi.fn(),
  createDashboard: vi.fn(),
}));

describe('DashboardViewer imperative ref', () => {
  it('exposes refresh() via ref', async () => {
    const { queryDashboard } = await import('@/api/dashboards');
    const ref = { current: null };
    const dash = { id: 'd1', name: 'D', definition: { widgets: [] }, refresh_interval_seconds: 30 };
    render(<DashboardViewer ref={ref} dashboard={dash} polling={false} />);
    await act(async () => { /* allow mount refresh */ });
    expect(queryDashboard).toHaveBeenCalledTimes(1);
    await act(async () => { ref.current.refresh(); });
    expect(queryDashboard).toHaveBeenCalledTimes(2);
  });
});
