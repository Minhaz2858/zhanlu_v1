import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

const getDashboard = vi.fn();
const getDashboardByConversation = vi.fn();
const undoDashboardEdit = vi.fn();
const queryDashboard = vi.fn();
const updateDashboard = vi.fn();
const createDashboard = vi.fn();

vi.mock('@/api/dashboards', () => ({
  getDashboard: (...a) => getDashboard(...a),
  getDashboardByConversation: (...a) => getDashboardByConversation(...a),
  undoDashboardEdit: (...a) => undoDashboardEdit(...a),
  queryDashboard: (...a) => queryDashboard(...a),
  updateDashboard: (...a) => updateDashboard(...a),
  createDashboard: (...a) => createDashboard(...a),
}));

// Recharts: minimal mock so any child chart that does render doesn't crash.
vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }) => <div>{children}</div>,
  CartesianGrid: () => <div />, Legend: () => <div />, Tooltip: () => <div />,
  LineChart: () => <div />, BarChart: () => <div />, PieChart: () => <div />,
  AreaChart: () => <div />, ScatterChart: () => <div />, RadarChart: () => <div />,
  RadialBarChart: () => <div />, Line: () => <div />, Bar: () => <div />, Pie: () => <div />,
  Area: () => <div />, Scatter: () => <div />, Radar: () => <div />, PolarGrid: () => <div />,
  PolarAngleAxis: () => <div />, PolarRadiusAxis: () => <div />, Cell: () => <div />,
}));

import DashboardDockPanel from './DashboardDockPanel';

const DASHBOARD = {
  id: 'd1', name: 'Sales', datasource_kb_id: 'kb1', refresh_interval_seconds: 30,
  can_edit: true, can_undo: false,
  definition: { widgets: [] },
};

beforeEach(() => {
  getDashboard.mockReset();
  getDashboardByConversation.mockReset();
  undoDashboardEdit.mockReset();
  queryDashboard.mockResolvedValue({ results: {}, refreshed_at: '2026-07-30T00:00:00Z' });
});

describe('DashboardDockPanel', () => {
  it('auto-opens the bound dashboard by conversation id', async () => {
    getDashboardByConversation.mockResolvedValue({ ...DASHBOARD });
    render(<DashboardDockPanel conversationId="conv1" onClose={() => { }} />);
    await waitFor(() => expect(getDashboardByConversation).toHaveBeenCalledWith('conv1'));
    await waitFor(() => expect(screen.getByText('Sales')).toBeInTheDocument());
  });

  it('uses explicit dashboardId when both are present', async () => {
    getDashboard.mockResolvedValue({ ...DASHBOARD, name: 'Explicit' });
    render(
      <DashboardDockPanel
        conversationId="conv1"
        dashboardId="d-explicit"
        onClose={() => { }}
      />,
    );
    await waitFor(() => expect(getDashboard).toHaveBeenCalledWith('d-explicit'));
    expect(getDashboardByConversation).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText('Explicit')).toBeInTheDocument());
  });

  it('shows Undo button enabled when can_undo is true', async () => {
    getDashboardByConversation.mockResolvedValueOnce({ ...DASHBOARD, can_undo: true });
    render(<DashboardDockPanel conversationId="conv1" onClose={() => { }} />);
    const undo = await screen.findByRole('button', { name: /undo/i });
    expect(undo).not.toBeDisabled();
  });

  it('shows Undo button disabled when can_undo is false', async () => {
    getDashboardByConversation.mockResolvedValueOnce({ ...DASHBOARD, can_undo: false });
    render(<DashboardDockPanel conversationId="conv1" onClose={() => { }} />);
    const undo = await screen.findByRole('button', { name: /undo/i });
    expect(undo).toBeDisabled();
  });

  it('hides Undo button when can_edit is false', async () => {
    getDashboardByConversation.mockResolvedValueOnce({ ...DASHBOARD, can_edit: false, can_undo: true });
    render(<DashboardDockPanel conversationId="conv1" onClose={() => { }} />);
    await waitFor(() => expect(screen.getByText('Sales')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /undo/i })).not.toBeInTheDocument();
  });

  it('invokes undoDashboardEdit and updates the dashboard when Undo is clicked', async () => {
    const restored = { ...DASHBOARD, name: 'Sales (reverted)', can_undo: false };
    getDashboardByConversation.mockResolvedValueOnce({ ...DASHBOARD, can_undo: true });
    undoDashboardEdit.mockResolvedValueOnce(restored);
    render(<DashboardDockPanel conversationId="conv1" onClose={() => { }} />);
    const undo = await screen.findByRole('button', { name: /undo/i });
    undo.click();
    await waitFor(() => expect(undoDashboardEdit).toHaveBeenCalledWith('d1'));
    await waitFor(() => expect(screen.getByText('Sales (reverted)')).toBeInTheDocument());
  });

  it('does not show a dashboard empty-state hint when no dashboard is bound (404)', async () => {
    getDashboardByConversation.mockRejectedValueOnce(new Error('404'));
    render(<DashboardDockPanel conversationId="conv-empty" onClose={() => { }} />);
    await waitFor(() => expect(getDashboardByConversation).toHaveBeenCalledWith('conv-empty'));
    expect(screen.queryByText(/no dashboard yet/i)).not.toBeInTheDocument();
  });
});
