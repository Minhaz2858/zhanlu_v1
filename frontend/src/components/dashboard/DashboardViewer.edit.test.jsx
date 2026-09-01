import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const queryDashboard = vi.fn();
const updateDashboard = vi.fn();
const createDashboard = vi.fn();
vi.mock('@/api/dashboards', () => ({
  queryDashboard: (...a) => queryDashboard(...a),
  updateDashboard: (...a) => updateDashboard(...a),
  createDashboard: (...a) => createDashboard(...a),
}));
vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }) => <div>{children}</div>,
  CartesianGrid: () => <div />, Legend: () => <div />, Tooltip: () => <div />,
  LineChart: () => <div />, BarChart: () => <div />, PieChart: () => <div />,
  AreaChart: () => <div />, ScatterChart: () => <div />, RadarChart: () => <div />,
  RadialBarChart: () => <div />, Line: () => <div />, Bar: () => <div />, Pie: () => <div />,
  Area: () => <div />, Scatter: () => <div />, Radar: () => <div />, PolarGrid: () => <div />,
  PolarAngleAxis: () => <div />, PolarRadiusAxis: () => <div />, Cell: () => <div />,
}));
// Mock the drawer so the viewer test stays focused on edit-mode orchestration.
vi.mock('./WidgetEditorDrawer', () => ({ default: vi.fn(() => null) }));

import DashboardViewer from './DashboardViewer';

const DASHBOARD = {
  id: 'd1', name: 'Sales', datasource_kb_id: 'kb1', project_id: 'p1', project: 'p1',
  refresh_interval_seconds: 30, created_by_id: 'u1',
  definition: { widgets: [{ id: 'w1', type: 'table', title: 'T', sql: 'SELECT 1 AS n', options: {} }] },
};

beforeEach(() => {
  queryDashboard.mockReset(); updateDashboard.mockReset(); createDashboard.mockReset();
  queryDashboard.mockResolvedValue({ results: {}, refreshed_at: '2026-07-29T00:00:00Z' });
});

describe('DashboardViewer edit mode', () => {
  it('hides Edit button when canEdit=false', () => {
    render(<DashboardViewer dashboard={DASHBOARD} polling={false} />);
    expect(screen.queryByText('Edit')).not.toBeInTheDocument();
  });

  it('shows Edit button when canEdit=true', () => {
    render(<DashboardViewer dashboard={DASHBOARD} canEdit polling={false} />);
    expect(screen.getByText('Edit')).toBeInTheDocument();
  });

  it('entering edit mode pauses polling (no queryDashboard calls after enter)', async () => {
    render(<DashboardViewer dashboard={DASHBOARD} canEdit polling={false} />);
    await waitFor(() => expect(queryDashboard).toHaveBeenCalled()); // initial mount
    queryDashboard.mockClear();
    fireEvent.click(screen.getByText('Edit'));
    await new Promise((r) => setTimeout(r, 10));
    expect(queryDashboard).not.toHaveBeenCalled();
  });

  it('Save calls updateDashboard with draft and onSaved', async () => {
    updateDashboard.mockResolvedValue({ ...DASHBOARD, name: 'renamed' });
    const onSaved = vi.fn();
    render(<DashboardViewer dashboard={DASHBOARD} canEdit polling={false} onSaved={onSaved} />);
    fireEvent.click(screen.getByText('Edit'));
    fireEvent.change(screen.getByDisplayValue('Sales'), { target: { value: 'renamed' } });
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(updateDashboard).toHaveBeenCalled());
    const [id, body] = updateDashboard.mock.calls[0];
    expect(id).toBe('d1');
    expect(body.name).toBe('renamed');
    expect(onSaved).toHaveBeenCalled();
  });

  it('Cancel with dirty shows confirm then discards', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<DashboardViewer dashboard={DASHBOARD} canEdit polling={false} />);
    fireEvent.click(screen.getByText('Edit'));
    fireEvent.change(screen.getByDisplayValue('Sales'), { target: { value: 'x' } });
    fireEvent.click(screen.getByText('Cancel'));
    expect(confirmSpy).toHaveBeenCalled();
    expect(screen.getByText('Edit')).toBeInTheDocument(); // back to view mode
    confirmSpy.mockRestore();
  });

  it('Save as copy calls createDashboard with (copy) name + draft', async () => {
    createDashboard.mockResolvedValue({ id: 'd2', name: 'Sales (copy)' });
    const onCopied = vi.fn();
    render(<DashboardViewer dashboard={DASHBOARD} canEdit polling={false} onCopied={onCopied} />);
    fireEvent.click(screen.getByText('Edit'));
    fireEvent.click(screen.getByText('Save as copy'));
    await waitFor(() => expect(createDashboard).toHaveBeenCalled());
    const body = createDashboard.mock.calls[0][0];
    expect(body.name).toBe('Sales (copy)');
    expect(body.datasource_kb_id).toBe('kb1');
    expect(body.definition).toEqual(DASHBOARD.definition);
    expect(onCopied).toHaveBeenCalled();
  });

  it('delete widget removes from draft', () => {
    render(<DashboardViewer dashboard={DASHBOARD} canEdit polling={false} />);
    fireEvent.click(screen.getByText('Edit'));
    fireEvent.click(screen.getByLabelText('Delete widget'));
    expect(screen.queryByText('T')).not.toBeInTheDocument();
  });

  it('renders drag handles in edit mode (native HTML5 DnD)', () => {
    render(<DashboardViewer dashboard={DASHBOARD} canEdit polling={false} />);
    fireEvent.click(screen.getByText('Edit'));
    expect(screen.getByLabelText('Drag')).toBeInTheDocument();
  });
});
