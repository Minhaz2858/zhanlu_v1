import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

const getDashboard = vi.fn();
vi.mock('@/api/dashboards', () => ({ getDashboard: (...a) => getDashboard(...a) }));
vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }) => <div>{children}</div>,
  CartesianGrid: () => <div />, Legend: () => <div />, Tooltip: () => <div />,
  LineChart: () => <div />, BarChart: () => <div />, PieChart: () => <div />,
  AreaChart: () => <div />, ScatterChart: () => <div />, RadarChart: () => <div />,
  RadialBarChart: () => <div />, Line: () => <div />, Bar: () => <div />, Pie: () => <div />,
  Area: () => <div />, Scatter: () => <div />, Radar: () => <div />, PolarGrid: () => <div />,
  PolarAngleAxis: () => <div />, PolarRadiusAxis: () => <div />, Cell: () => <div />,
}));

import DashboardPopup from './DashboardPopup';

const DASHBOARD = {
  id: 'd1', name: 'Sales', datasource_kb_id: 'kb1', refresh_interval_seconds: 30,
  can_edit: true,
  definition: { widgets: [{ id: 'w1', type: 'table', title: 'T', sql: 'SELECT 1 AS n', options: {} }] },
};

beforeEach(() => { getDashboard.mockReset(); });

describe('DashboardPopup canEdit wiring', () => {
  it('shows Edit button when dashboard.can_edit is true', async () => {
    getDashboard.mockResolvedValue(DASHBOARD);
    render(<DashboardPopup dashboardId="d1" onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('Edit')).toBeInTheDocument());
  });

  it('hides Edit button when dashboard.can_edit is false', async () => {
    getDashboard.mockResolvedValue({ ...DASHBOARD, can_edit: false });
    render(<DashboardPopup dashboardId="d1" onClose={() => {}} />);
    await waitFor(() => expect(getDashboard).toHaveBeenCalled());
    // give the viewer a tick to render
    await new Promise((r) => setTimeout(r, 10));
    expect(screen.queryByText('Edit')).not.toBeInTheDocument();
  });
});
